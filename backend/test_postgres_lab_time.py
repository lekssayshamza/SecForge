"""Real PostgreSQL timing races, only on the owned disposable harness."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned disposable PostgreSQL runner', allow_module_level=True)

import threading
from datetime import timedelta
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select, text
from app import labs, reconciliation
from app.lab_time import utc_now, as_utc
from app.lab_time_migration import migrate
from app.models import User, Room, LabInstance, RoomProgress
from app.schemas import LabExtendRequest
from test_postgres_lab_concurrency import pg_sessions, observe_lock_wait


def seed(maker, now, minutes=30):
    with maker() as db:
        user=User(username='timed_learner',email='timing@example.com',password_hash='unused',role='learner');db.add(user);db.flush()
        creator=User(username='timed_creator',email='timed-creator@example.com',password_hash='unused',role='creator');db.add(creator);db.flush()
        room=Room(creator_id=creator.id,name='Timing race fixture',status='published',category='Linux',difficulty='easy');db.add(room);db.flush()
        lab=LabInstance(room_id=room.id,learner_id=user.id,status='running',session_started_at=now-timedelta(minutes=60-minutes),expires_at=now+timedelta(minutes=minutes));db.add(lab);db.flush()
        db.add(RoomProgress(room_id=room.id,learner_id=user.id,points_earned=75));db.commit();return user.id,room.id,lab.id


@pytest.mark.parametrize('first_stop', [False,True])
def test_row_locked_extension_serializes_duplicate_or_manual_stop(pg_sessions,monkeypatch,first_stop):
    now=utc_now();monkeypatch.setattr(labs,'utc_now',lambda:now)
    uid,rid,lid=seed(pg_sessions,now)
    held=threading.Event();release=threading.Event();ready=threading.Event();pids={};results={};errors=[]
    def actor(name):
        try:
            with pg_sessions() as db:
                pids[name]=db.scalar(text('SELECT pg_backend_pid()'));user=db.get(User,uid)
                if name=='first':
                    original=db.scalar
                    def scalar(stmt,*args,**kw):
                        result=original(stmt,*args,**kw)
                        if getattr(stmt,'_for_update_arg',None) is not None:
                            held.set();assert release.wait(10)
                        return result
                    db.scalar=scalar
                else:ready.set()
                tasks=BackgroundTasks()
                try:
                    result=labs.stop_lab(rid,tasks,user,db) if first_stop and name=='first' else labs.extend_lab(rid,LabExtendRequest(lab_id=lid,expected_extensions_used=0),user,db)
                    results[name]=(200,result.extensions_used,result.expires_at,len(tasks.tasks))
                except HTTPException as exc:results[name]=(exc.status_code,)
        except BaseException as exc:errors.append(type(exc).__name__)
    first=threading.Thread(target=actor,args=('first',));second=threading.Thread(target=actor,args=('second',));first.start()
    try:
        assert held.wait(8);second.start();assert ready.wait(8)
        observe_lock_wait(pg_sessions,pids['second'],pids['first']);assert 'second' not in results
    finally:
        release.set();first.join(22)
        if second.ident is not None:second.join(22)
    assert not first.is_alive() and not second.is_alive() and not errors
    if first_stop:
        assert results['first'][3]==1 and results['second'][0]==409
    else:
        assert results['first'][:3]==results['second'][:3] and results['first'][1]==1
    with pg_sessions() as db:
        lab=db.get(LabInstance,lid);assert lab.extensions_used==(0 if first_stop else 1)
        assert lab.expires_at==now+timedelta(minutes=30 if first_stop else 90)
        assert db.scalar(select(RoomProgress.points_earned))==75


def test_expiration_claim_then_stop_schedules_one_cleanup(pg_sessions,monkeypatch):
    now=utc_now();monkeypatch.setattr(labs,'utc_now',lambda:now);monkeypatch.setattr(reconciliation,'utc_now',lambda:now)
    uid,rid,lid=seed(pg_sessions,now,0)
    worker=reconciliation.ReconciliationWorker();assert worker._expiration_step()==[lid]
    with pg_sessions() as db:
        tasks=BackgroundTasks();result=labs.stop_lab(rid,tasks,db.get(User,uid),db)
        assert result.status=='stopping' and not tasks.tasks and result.expired_at==now
        with pytest.raises(HTTPException) as error:labs.extend_lab(rid,LabExtendRequest(lab_id=lid,expected_extensions_used=0),db.get(User,uid),db)
        assert error.value.status_code==409


def test_extension_wins_before_stale_expiration_claim(pg_sessions,monkeypatch):
    now=utc_now();monkeypatch.setattr(labs,'utc_now',lambda:now)
    uid,rid,lid=seed(pg_sessions,now)
    with pg_sessions() as stale:
        cached=stale.get(LabInstance,lid)
        with pg_sessions() as fresh:labs.extend_lab(rid,LabExtendRequest(lab_id=lid,expected_extensions_used=0),fresh.get(User,uid),fresh)
        assert reconciliation.ReconciliationWorker()._claim_lab_for_stop(stale,lid,expired_before=now+timedelta(minutes=30)) is None
        stale.refresh(cached);assert cached.status=='running' and cached.extensions_used==1


@pytest.mark.parametrize('historical_status', ['running','stopped'])
def test_database_quota_and_migration_utc_history(pg_sessions,historical_status):
    from sqlalchemy.exc import IntegrityError
    now=utc_now();uid,rid,lid=seed(pg_sessions,now)
    with pg_sessions() as db:
        with pytest.raises(IntegrityError):
            db.execute(text('UPDATE lab_instances SET extensions_used=3 WHERE id=:id'),dict(id=lid));db.commit()
        db.rollback()
        db.execute(text("UPDATE lab_instances SET status=:status WHERE id=:id"),dict(id=lid,status=historical_status));db.commit()
        before=db.get(LabInstance,lid).expires_at
        db.execute(text('ALTER TABLE lab_instances DROP CONSTRAINT ck_lab_extension_quota'))
        for column in ['session_started_at','expired_at','extensions_used']:db.execute(text(f'ALTER TABLE lab_instances DROP COLUMN {column}'))
        db.execute(text("ALTER TABLE lab_instances ALTER COLUMN expires_at TYPE TIMESTAMP WITHOUT TIME ZONE USING expires_at AT TIME ZONE 'UTC'"));db.commit()
        db.execute(text("SET TIME ZONE 'Europe/Stockholm'"))
        assert migrate(db.connection(),now);db.commit();assert not migrate(db.connection(),now+timedelta(hours=1));db.commit()
        db.expire_all();lab=db.get(LabInstance,lid)
        assert lab.expires_at.tzinfo is not None and lab.extensions_used==0
        if historical_status=='stopped':assert lab.expires_at==before and lab.session_started_at is None
        else:assert lab.expires_at==now+timedelta(minutes=60) and lab.session_started_at==now
        assert db.scalar(select(RoomProgress.points_earned))==75
