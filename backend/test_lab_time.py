"""Offline learner timing tests; all infrastructure operations are faked."""
from datetime import datetime, timedelta
from unittest.mock import Mock
import asyncio
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select, text, create_engine
from app import labs, reconciliation, vpn_authz
from app.lab_time import utc_now, as_utc, INITIAL_TIME
from app.lab_time_migration import migrate
from app.models import LabInstance, Room, RoomTemplate, MachineTemplate, RoomProgress, VpnIdentity, User, BuilderEnvironment
from app.schemas import LabExtendRequest, LabStartRequest
from app.ops_lock import single_flight, lab_lock_key
from app.lab_manager import LabManager, TerraformError
from test_profiles import env


@pytest.fixture
def clock(monkeypatch):
    now = utc_now().replace(microsecond=0)
    monkeypatch.setattr(labs, 'utc_now', lambda: now)
    monkeypatch.setattr(reconciliation, 'utc_now', lambda: now)
    return now


def make(env, now, minutes=30, status='running', used=0):
    with env.maker() as db:
        room=Room(name='Timing fixture', creator_id=env.ids['creator'], category='Linux', difficulty='easy', status='published')
        db.add(room);db.flush()
        start=now-timedelta(minutes=60+60*used-minutes)
        lab=LabInstance(room_id=room.id,learner_id=env.ids['learner'],status=status,session_started_at=start,expires_at=now+timedelta(minutes=minutes),extensions_used=used,private_ip='10.0.1.24')
        db.add(lab);db.flush()
        db.add(RoomProgress(room_id=room.id,learner_id=env.ids['learner'],points_earned=75))
        db.commit();return room.id,lab.id


def extend(env, room, lab, expected=0, role='learner'):
    return env.client.post(f'/api/rooms/{room}/lab/extend',headers=env.headers[role],json=dict(lab_id=lab,expected_extensions_used=expected))


def test_initial_hour_and_fresh_session_are_server_allocated(env, clock, monkeypatch):
    monkeypatch.setattr(labs, 'provision_lab_background', lambda *args: None)
    with env.maker() as db:
        room=Room(name='Pinned timing',creator_id=env.ids['creator'],category='Linux',difficulty='easy',status='published');db.add(room);db.flush()
        template=RoomTemplate(room_id=room.id,created_by=env.ids['creator'],version=1,is_published=True);db.add(template);db.flush()
        db.add(MachineTemplate(room_template_id=template.id,ami_id='ami-offline',status='available'));db.commit();rid=room.id
    def start():return env.client.post(f'/api/rooms/{rid}/lab',headers=env.headers['learner'],json={'ttl_hours':4})
    first=start();assert first.status_code==201
    data=first.json();assert as_utc(datetime.fromisoformat(data['expires_at']))==clock+INITIAL_TIME
    assert data['session_started_at'].endswith('Z') and data['server_time'].endswith('Z') and data['extensions_used']==0
    with env.maker() as db:
        lab=db.get(LabInstance,data['id']);lab.status='stopped';lab.extensions_used=2;db.commit()
    second=start();assert second.status_code==201 and second.json()['id']!=data['id'] and second.json()['extensions_used']==0
    assert second.json()['expires_at']==data['expires_at']


@pytest.mark.parametrize('minutes', [60,31])
def test_early_extension_rejected(env,clock,minutes):
    room,lab=make(env,clock,minutes);assert extend(env,room,lab).status_code==409


def test_two_extensions_replay_and_cap(env,clock,monkeypatch):
    room,lid=make(env,clock)
    first=extend(env,room,lid);assert first.status_code==200 and first.json()['extensions_used']==1
    assert extend(env,room,lid).json()['expires_at']==first.json()['expires_at']
    later=clock+timedelta(minutes=60);monkeypatch.setattr(labs,'utc_now',lambda:later)
    second=extend(env,room,lid,1);assert second.status_code==200 and second.json()['extensions_used']==2
    assert extend(env,room,lid,1).json()['expires_at']==second.json()['expires_at']
    assert extend(env,room,lid,2).status_code==409
    with env.maker() as db:
        lab=db.get(LabInstance,lid);assert as_utc(lab.expires_at)-as_utc(lab.session_started_at)==timedelta(minutes=180)
        assert db.scalar(select(RoomProgress.points_earned))==75


@pytest.mark.parametrize('status,minutes',[('running',0),('running',-1),('stopping',20),('stopped',20),('failed',20),('pending',20),('provisioning',20)])
def test_expired_and_invalid_states_reject_even_replay(env,clock,status,minutes):
    room,lab=make(env,clock,minutes,status,1);assert extend(env,room,lab,0).status_code==409


def test_ownership_roles_stale_session_and_untrusted_fields(env,clock):
    room,lab=make(env,clock)
    assert extend(env,room,lab,0,'creator').status_code==404
    assert extend(env,room,lab,0,'admin').status_code==404
    assert extend(env,room,lab+1000).status_code==404
    assert env.client.post(f'/api/rooms/{room}/lab/extend',json={'lab_id':lab,'expected_extensions_used':0}).status_code in (401,403)
    with env.maker() as db:db.get(LabInstance,lab).learner_id=env.ids['creator'];db.commit()
    assert extend(env,room,lab).status_code==404
    assert env.client.post(f'/api/rooms/{room}/lab/extend',headers=env.headers['learner'],json={'lab_id':lab,'expected_extensions_used':0,'expires_at':'2099-01-01'}).status_code==422


def test_creator_test_lab_and_legacy_overlong_deadline_cannot_extend(env,clock):
    room,lid=make(env,clock)
    with env.maker() as db:db.get(LabInstance,lid).session_started_at=None;db.commit()
    assert extend(env,room,lid).status_code==409
    with env.maker() as db:db.get(LabInstance,lid).session_started_at=clock-timedelta(hours=3);db.commit()
    assert extend(env,room,lid).status_code==409


def test_database_failure_is_sanitized(env,clock,monkeypatch):
    from sqlalchemy.exc import OperationalError
    room,lid=make(env,clock)
    def fail(*args,**kwargs):raise OperationalError('private SQL',{},Exception('secret diagnostic'))
    monkeypatch.setattr('sqlalchemy.orm.Session.commit',fail)
    response=extend(env,room,lid);assert response.status_code==503 and 'private' not in response.text and 'secret' not in response.text


@pytest.mark.parametrize('status',['pending','provisioning','running'])
def test_browser_closed_expiration_recovery_revokes_and_preserves_metadata(env,clock,monkeypatch,status):
    room,lid=make(env,clock,0,status)
    with env.maker() as db:
        lab=db.get(LabInstance,lid);lab.terraform_state_path='/offline/workspace';lab.instance_id='i-offline';lab.security_group_id='sg-offline'
        db.add(VpnIdentity(user_id=env.ids['learner'],common_name='sf-offline',vpn_ip='10.8.0.65'));db.commit()
    monkeypatch.setattr(reconciliation,'SessionLocal',env.maker)
    seen=[]
    monkeypatch.setattr(reconciliation,'sync_vpn_authz',lambda db:seen.append(vpn_authz.desired_pairs(db)))
    worker=reconciliation.ReconciliationWorker();assert worker._expiration_step()==[lid]
    assert ('10.8.0.65','10.0.1.24') not in seen[-1]
    with env.maker() as db:
        lab=db.get(LabInstance,lid);assert lab.status=='stopping' and lab.expired_at is not None
        assert lab.terraform_state_path=='/offline/workspace' and lab.instance_id=='i-offline' and lab.security_group_id=='sg-offline'
        assert db.scalar(select(RoomProgress.points_earned))==75
    # Fresh worker after restart rediscovers the committed Stop; no browser.
    assert reconciliation.ReconciliationWorker()._expiration_step()==[lid]


def test_deferred_expiration_destroy_and_cleanup_retry_are_idempotent(env,clock,monkeypatch):
    room,lid=make(env,clock,0)
    monkeypatch.setattr(reconciliation,'SessionLocal',env.maker);monkeypatch.setattr(labs,'SessionLocal',env.maker)
    monkeypatch.setattr(reconciliation,'sync_vpn_authz',lambda db:None)
    monkeypatch.setattr('app.lab_manager.sync_vpn_authz',lambda db:None)
    worker=reconciliation.ReconciliationWorker();assert worker._expiration_step()==[lid]
    attempts=[]
    def destroy(self,lab,kind):
        attempts.append(lab.id)
        if len(attempts)==1:raise TerraformError('transient offline init failure')
        return '/offline/workspace'
    monkeypatch.setattr(LabManager,'_destroy_environment',destroy);monkeypatch.setattr(LabManager,'_cleanup_workspace',lambda *args:None)
    with single_flight(lab_lock_key(lid)):
        labs.destroy_lab_background(lid);assert not attempts
    labs.destroy_lab_background(lid)
    with env.maker() as db:
        lab=db.get(LabInstance,lid);assert lab.status=='stopping' and 'Cleanup retry required' in lab.error_message
    assert worker._expiration_step()==[]  # Bounded retry spacing after real failure.
    labs.destroy_lab_background(lid);labs.destroy_lab_background(lid)
    with env.maker() as db:
        assert db.get(LabInstance,lid).status=='stopped' and db.scalar(select(RoomProgress.points_earned))==75
    assert attempts==[lid,lid]


def test_stale_expiration_selection_cannot_stop_extended_lab(env,clock):
    room,lid=make(env,clock)
    with env.maker() as db:
        assert labs.extend_lab(room,LabExtendRequest(lab_id=lid,expected_extensions_used=0),db.get(User,env.ids['learner']),db).extensions_used==1
        assert reconciliation.ReconciliationWorker()._claim_lab_for_stop(db,lid,expired_before=clock+timedelta(minutes=30)) is None
        db.refresh(db.get(LabInstance,lid));assert db.get(LabInstance,lid).status=='running'


def test_expired_pending_never_starts_terraform(env,clock,monkeypatch):
    room,lid=make(env,clock,0,'pending')
    monkeypatch.setattr('app.lab_manager.utc_now',lambda:clock)
    terraform=Mock();monkeypatch.setattr(LabManager,'_run_terraform',terraform)
    with env.maker() as db:
        LabManager().start_lab_instance(db.get(LabInstance,lid),db)
        assert db.get(LabInstance,lid).status=='stopping' and db.get(LabInstance,lid).expired_at is not None
    terraform.assert_not_called()


def test_timed_provisioning_failure_preserves_recoverable_cleanup(env,clock,monkeypatch):
    room,lid=make(env,clock,30,'pending')
    monkeypatch.setattr('app.templates.resolve_lab_ami',lambda *args:'ami-offline')
    monkeypatch.setattr(LabManager,'_prepare_lab_workspace',lambda *args:'/offline/workspace')
    def fail(*args,**kwargs):raise TerraformError('controlled init failure')
    monkeypatch.setattr(LabManager,'_run_terraform',fail)
    with env.maker() as db:
        lab=db.get(LabInstance,lid)
        with pytest.raises(TerraformError):LabManager().start_lab_instance(lab,db)
        assert lab.status=='stopping' and lab.terraform_state_path and lab.error_message.startswith('Cleanup retry required:')


def test_single_worker_fast_tick_does_not_wait_for_destroy_or_duplicate_it(monkeypatch):
    import threading
    async def scenario():
        worker=reconciliation.ReconciliationWorker();worker.config['lab_expiration_interval_seconds']=.01
        loop=asyncio.get_running_loop();started=asyncio.Event();checked_again=asyncio.Event();release=threading.Event()
        calls=[];ticks=[]
        def step():
            ticks.append(1)
            if len(ticks)>1:loop.call_soon_threadsafe(checked_again.set)
            return [999]
        def held_destroy(lid):
            calls.append(lid);loop.call_soon_threadsafe(started.set);assert release.wait(5)
        monkeypatch.setattr(worker,'_expiration_step',step)
        monkeypatch.setattr(worker,'_run_loop',lambda:worker._stop_event.wait())
        monkeypatch.setattr(worker,'_run_orphan_loop',lambda:worker._orphan_stop_event.wait())
        monkeypatch.setattr(labs,'destroy_lab_background',held_destroy)
        try:
            await worker.start();task=worker._expiration_task;await worker.start();assert worker._expiration_task is task
            await asyncio.wait_for(started.wait(),3);await asyncio.wait_for(checked_again.wait(),3)
            assert calls==[999]  # A second tick cannot submit a second destroy.
        finally:
            release.set();await asyncio.wait_for(worker.stop(),4)
        assert not worker._expiration_cleanups and worker._expiration_task is None
    asyncio.run(scenario())


def test_failed_revocation_retries_without_affecting_other_identity_or_builder(env,clock,monkeypatch):
    room,lid=make(env,clock,0)
    with env.maker() as db:
        other=User(username='other_timing',email='other@example.com',password_hash='unused',role='learner');db.add(other);db.flush()
        for uid,cn,ip in [(env.ids['learner'],'sf-a','10.8.0.65'),(other.id,'sf-b','10.8.0.66'),(env.ids['creator'],'sf-c','10.8.0.67')]:
            db.add(VpnIdentity(user_id=uid,common_name=cn,vpn_ip=ip))
        db.add(LabInstance(room_id=room,learner_id=other.id,status='running',private_ip='10.0.1.26',expires_at=clock+timedelta(hours=1)))
        db.add(BuilderEnvironment(room_id=room,creator_id=env.ids['creator'],status='running',private_ip='10.0.1.25',terraform_state_path='/offline/builder',expires_at=clock+timedelta(hours=4)))
        db.commit()
    monkeypatch.setattr(reconciliation,'SessionLocal',env.maker);calls=[]
    def sync(db):
        calls.append(vpn_authz.desired_pairs(db))
        if len(calls)==1:raise vpn_authz.VpnAuthorizationError('controlled gateway outage')
    monkeypatch.setattr(reconciliation,'sync_vpn_authz',sync)
    worker=reconciliation.ReconciliationWorker();assert worker._expiration_step()==[lid];assert worker._expiration_step()==[lid]
    assert len(calls)==2
    for pairs in calls:
        assert ('10.8.0.65','10.0.1.24') not in pairs
        assert ('10.8.0.66','10.0.1.26') in pairs and ('10.8.0.67','10.0.1.25') in pairs


def test_migration_grace_and_history_are_idempotent():
    from datetime import datetime, timezone
    engine=create_engine('sqlite://')
    now=datetime(2026,10,11,12,tzinfo=timezone.utc)
    with engine.begin() as c:
        c.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY,role VARCHAR(20))'))
        c.execute(text('CREATE TABLE lab_instances (id INTEGER PRIMARY KEY,learner_id INTEGER,status VARCHAR(20),expires_at DATETIME)'))
        c.execute(text("INSERT INTO users VALUES (1,'learner'),(2,'creator')"))
        for lid,owner,status,deadline in [(1,1,'running',now-timedelta(hours=1)),(2,1,'running',now+timedelta(hours=4)),(3,1,'stopped',now-timedelta(days=1)),(4,2,'running',now-timedelta(minutes=1))]:
            c.execute(text('INSERT INTO lab_instances VALUES (:id,:owner,:status,:deadline)'),dict(id=lid,owner=owner,status=status,deadline=deadline))
        assert migrate(c,now);assert not migrate(c,now+timedelta(hours=2))
        rows=c.execute(text('SELECT * FROM lab_instances ORDER BY id')).mappings().all()
        assert datetime.fromisoformat(rows[0]['expires_at'])==now+INITIAL_TIME
        assert datetime.fromisoformat(rows[1]['expires_at'])==now+timedelta(hours=4)
        assert rows[2]['session_started_at'] is None and rows[3]['session_started_at'] is None
        assert all(row['extensions_used']==0 for row in rows)
    engine.dispose()
