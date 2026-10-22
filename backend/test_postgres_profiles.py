"""Profile races and migrations on the harness-owned PostgreSQL only."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned disposable PostgreSQL runner', allow_module_level=True)

import threading
from fastapi import HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from app.models import User, Room, RoomProgress
from app.schemas import ProfileUpdate
from app.users import update_profile
from app.profile_migration import migrate
from test_postgres_lab_concurrency import pg_sessions


def seed(maker):
    with maker() as db:
        users=[User(username=n,email=n+'@example.com',password_hash='unused',role='learner') for n in ('first_profile','second_profile')]
        db.add_all(users);db.flush()
        room=Room(creator_id=users[0].id,name='Preserved owned room',status='published',category='Linux',difficulty='easy')
        db.add(room);db.flush()
        db.add(RoomProgress(room_id=room.id,learner_id=users[0].id,points_earned=75))
        db.commit();return [u.id for u in users],room.id


def test_concurrent_profile_renames_award_one_name_without_changing_ids(pg_sessions):
    ids,_=seed(pg_sessions)
    ready=threading.Barrier(2,timeout=10)
    results,errors={},[]
    def actor(uid):
        try:
            with pg_sessions() as db:
                user=db.get(User,uid)
                commit=db.commit
                def held_commit():ready.wait();commit()
                db.commit=held_commit
                try:
                    update_profile(ProfileUpdate(username=' SAME_NAME ',display_name='New display'),user,db)
                    results[uid]=200
                except HTTPException as exc:results[uid]=exc.status_code
        except BaseException as exc:errors.append(exc)
    workers=[threading.Thread(target=actor,args=(uid,)) for uid in ids]
    for worker in workers:worker.start()
    for worker in workers:worker.join(25)
    assert not any(w.is_alive() for w in workers) and not errors
    assert sorted(results.values())==[200,409]
    with pg_sessions() as db:
        users=db.scalars(select(User).order_by(User.id)).all()
        assert [u.id for u in users]==ids
        assert sum(u.username=='same_name' for u in users)==1
        loser=next(u for u in users if results[u.id]==409)
        assert loser.display_name is None
        assert db.scalar(select(RoomProgress.points_earned))==75


def test_postgres_case_insensitive_index_is_enforced(pg_sessions):
    ids,_=seed(pg_sessions)
    with pg_sessions() as db:
        db.add(User(username='FIRST_PROFILE',email='duplicate@example.com',password_hash='unused',role='learner'))
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()
        assert db.get(User,ids[0]).username=='first_profile'


def remove_new_columns(db):
    db.execute(text('DROP INDEX uq_users_username_ci'))
    for column in ['display_name','avatar_style','avatar_color']:
        db.execute(text(f'ALTER TABLE users DROP COLUMN {column}'))


def test_postgres_additive_profile_migration_preserves_rooms_progress_and_accounts(pg_sessions):
    ids,room=seed(pg_sessions)
    with pg_sessions() as db:
        remove_new_columns(db);db.commit()
        assert migrate(db.connection());db.commit()
        assert not migrate(db.connection());db.commit()
        user=db.get(User,ids[0])
        assert user.username=='first_profile' and user.role=='learner' and user.password_hash=='unused'
        assert user.display_name is None and (user.avatar_style,user.avatar_color)==('initials','red')
        assert db.get(Room,room).creator_id==ids[0]
        assert db.scalar(select(RoomProgress.points_earned))==75
        db.add(User(username='FIRST_PROFILE',email='duplicate@example.com',password_hash='unused',role='learner'))
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()


def test_postgres_migration_collision_refuses_without_resetting_accounts(pg_sessions):
    ids,_=seed(pg_sessions)
    with pg_sessions() as db:
        remove_new_columns(db)
        db.execute(text("UPDATE users SET username='FIRST_PROFILE' WHERE id=:id"),dict(id=ids[1]))
        db.commit()
        with pytest.raises(RuntimeError):migrate(db.connection())
        db.rollback()
        assert 'display_name' not in {c['name'] for c in inspect(db.connection()).get_columns('users')}
        assert db.scalar(text('SELECT count(*) FROM users'))==2
        assert db.scalar(select(RoomProgress.points_earned))==75

def test_postgres_v2_migration_preserves_auth_and_profile(pg_sessions):
    from app.profile_v2_migration import migrate as migration
    ids,_=seed(pg_sessions)
    with pg_sessions() as db:
        db.execute(text('ALTER TABLE users DROP COLUMN bio'));db.execute(text('ALTER TABLE users DROP COLUMN avatar_version'));db.commit()
        assert migration(db.connection());db.commit();assert not migration(db.connection());db.commit()
        u=db.get(User,ids[0]);assert u.bio is None and u.avatar_version is None and u.password_hash=='unused' and u.role=='learner'
        assert db.scalar(select(RoomProgress.points_earned))==75


def test_postgres_real_ledger_learning_and_private_history(pg_sessions):
    from types import SimpleNamespace
    from fastapi import Response
    from test_profile_v2 import seed_history
    from app.learning import overview,activity
    ids,_=seed(pg_sessions)
    fixture=SimpleNamespace(maker=pg_sessions,ids={'creator':ids[1],'learner':ids[0]})
    seed_history(fixture,completed=5)
    with pg_sessions() as db:
        user=db.get(User,ids[0]);result=overview(Response(),user,db)
        assert result.stats.points==250 and result.stats.rooms_completed==5 and result.stats.questions_solved==10
        assert all(b.earned for b in result.achievements)
        assert activity(Response(),1,2,user,db).total==15
        other=overview(Response(),db.get(User,ids[1]),db);assert other.stats.points==0 and not other.activity


def test_postgres_concurrent_avatar_replacements_leave_one_owned_file(pg_sessions,tmp_path,monkeypatch):
    from app import avatars
    from test_profile_v2 import picture
    ids,_=seed(pg_sessions);root=tmp_path/'avatars';root.mkdir(mode=0o700);monkeypatch.setattr(avatars.settings,'avatar_storage_dir',str(root))
    ready=threading.Barrier(2,timeout=10);errors=[]
    def actor():
        try:
            with pg_sessions() as db:
                ready.wait();avatars.save_picture(picture(),'image/png',ids[0],0,db)
        except BaseException as exc:errors.append(exc)
    workers=[threading.Thread(target=actor) for _ in range(2)]
    for worker in workers:worker.start()
    for worker in workers:worker.join(20)
    assert not any(w.is_alive() for w in workers) and not errors
    with pg_sessions() as db:
        key=db.get(User,ids[0]).avatar_version
        assert [p.name for p in root.glob('*.png')]==[key+'.png']


def test_postgres_leaderboard_utc_rank_and_current_user(pg_sessions,monkeypatch):
    from datetime import datetime,timezone
    from fastapi import Response
    from app import leaderboard as rankings
    from app.models import QuestionSolve,ChallengeQuestion
    now=datetime(2026,10,13,12,tzinfo=timezone.utc);monkeypatch.setattr(rankings,'utc_now',lambda:now)
    ids,room=seed(pg_sessions)
    with pg_sessions() as db:
        for i in (1,2):db.add(ChallengeQuestion(id=i,room_id=room,question_text='Synthetic question',answer_salt='s'*32,answer_digest='d'*64,points=25,display_order=i))
        db.flush();db.add_all([QuestionSolve(learner_id=ids[0],question_id=1,points_awarded=75,solved_at=datetime(2026,10,11,23,59,59)),QuestionSolve(learner_id=ids[0],question_id=2,points_awarded=25,solved_at=datetime(2026,10,12)),QuestionSolve(learner_id=ids[1],question_id=1,points_awarded=50,solved_at=datetime(2026,10,12))]);db.commit()
        assert rankings.leaderboard(Response(),'all-time',1,20,db).items[0].username=='first_profile'
        assert rankings.leaderboard(Response(),'weekly',1,20,db).items[0].username=='second_profile'
        own=rankings.my_rank(Response(),'weekly',db.get(User,ids[0]),db);assert own.rank==2 and own.points==25
        public=rankings.public_learner(Response(),'first_profile',db);assert public.points==100 and public.questions_solved==2
        db.add(QuestionSolve(learner_id=ids[0],question_id=1,points_awarded=25))
        with pytest.raises(IntegrityError):db.commit()
        db.rollback();assert rankings.leaderboard(Response(),'all-time',1,20,db).items[0].points==100


def test_postgres_ranking_indexes_migrate_without_rewriting_data(pg_sessions):
    from app.leaderboard_migration import migrate,INDEXES
    ids,room=seed(pg_sessions)
    with pg_sessions() as db:
        for _,name,_,_ in INDEXES:db.execute(text('DROP INDEX '+name))
        db.commit();assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        assert db.get(User,ids[0]).username=='first_profile' and db.scalar(select(RoomProgress.points_earned))==75
        for table,name,columns,_ in INDEXES:assert next(i for i in inspect(db.connection()).get_indexes(table) if i['name']==name)['column_names']==columns


def test_postgres_thousands_of_rankings_one_statement_and_query_plan(pg_sessions):
    from datetime import datetime,timezone
    from sqlalchemy import event
    from fastapi import Response
    from app import leaderboard as rankings
    from app.models import QuestionSolve,ChallengeQuestion
    ids,room=seed(pg_sessions)
    with pg_sessions() as db:
        q=ChallengeQuestion(room_id=room,question_text='Synthetic question',answer_salt='s'*32,answer_digest='d'*64,points=25,display_order=1);db.add(q);db.flush()
        users=[User(username=f'pg_scale_{i}',email=f'pg_scale_{i}@example.com',password_hash='unused',role='learner') for i in range(2500)];db.add_all(users);db.flush()
        db.add_all([QuestionSolve(learner_id=u.id,question_id=q.id,points_awarded=i%1000+1) for i,u in enumerate(users)]);db.commit()
        statements=[]
        def count(*args):statements.append(args[2])
        event.listen(db.bind,'before_cursor_execute',count)
        try:result=rankings.leaderboard(Response(),'all-time',100,20,db)
        finally:event.remove(db.bind,'before_cursor_execute',count)
        assert result.total==2500 and len(result.items)==20 and len(statements)==1
        ranked,_,_=rankings.queries('all-time',datetime.now(timezone.utc))
        sql=str(select(ranked).order_by(ranked.c.rank).limit(20).compile(db.bind,compile_kwargs={'literal_binds':True}))
        plan=db.scalar(text('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+sql))
        assert plan[0]['Plan']['Actual Rows']==20 and plan[0]['Execution Time']<5000
        assert db.get(User,ids[0]).username=='first_profile'


def test_postgres_privacy_migration_preserves_accounts_and_explicit_consent(pg_sessions):
    from app.profile_privacy_migration import migrate as privacy
    ids,room=seed(pg_sessions)
    with pg_sessions() as db:
        db.execute(text('ALTER TABLE users DROP COLUMN avatar_public'));db.execute(text('ALTER TABLE users DROP COLUMN bio_public'));db.commit()
        assert privacy(db.connection());db.commit();assert not privacy(db.connection());db.commit()
        u=db.get(User,ids[0]);assert not u.avatar_public and not u.bio_public
        assert u.role=='learner' and u.password_hash=='unused' and db.get(Room,room).creator_id==u.id
        u.avatar_public=True;u.bio_public=True;db.commit()
        assert not privacy(db.connection());db.commit();db.refresh(u)
        assert u.avatar_public and u.bio_public and db.scalar(select(RoomProgress.points_earned))==75
