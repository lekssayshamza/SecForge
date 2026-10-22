"""Real PostgreSQL scoring interleavings; only the owned disposable harness may activate."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned PostgreSQL concurrency runner',allow_module_level=True)

import threading
from fastapi import HTTPException
from sqlalchemy import select, text, inspect, func
from sqlalchemy.exc import IntegrityError
from app import challenges
from app.models import ChallengeQuestion, QuestionSolve, Room, RoomProgress, User
from app.schemas import AnswerSubmission, QuestionCreate, QuestionUpdate
from app.challenge_migration import migrate, TABLES
from test_postgres_lab_concurrency import pg_sessions, observe_lock_wait


def seed(maker,second_question=False,second_learner=False):
    with maker() as db:
        creator=User(username='question_creator',email='creator@example.com',role='creator',password_hash='unused')
        learner=User(username='question_learner',email='learner@example.com',role='learner',password_hash='unused')
        db.add_all([creator,learner]);db.flush()
        room=Room(creator_id=creator.id,name='Concurrent challenge',category='Web',difficulty='easy',status='draft')
        db.add(room);db.commit()
        q=challenges.create_question(room.id,QuestionCreate(question_text='First task',answer='offline flag',points=100),creator,db)
        q2=challenges.create_question(room.id,QuestionCreate(question_text='Second task',answer='offline flag',points=75),creator,db) if second_question else q
        room.status='published';db.commit()
        other=User(username='second_learner',email='second@example.com',role='learner',password_hash='unused') if second_learner else learner
        if second_learner:db.add(other);db.commit()
        return room.id,creator.id,learner.id,other.id,q.id,q2.id


def interleave(maker,first_call,second_call,lock_entity=None):
    held,release,ready=threading.Event(),threading.Event(),threading.Event()
    pids,results,errors={},{},[]
    def actor(name,call):
        try:
            with maker() as db:
                pids[name]=db.scalar(text('SELECT pg_backend_pid()'))
                if name=='first':
                    scalar=db.scalar
                    def pause(statement,*args,**kwargs):
                        result=scalar(statement,*args,**kwargs)
                        if getattr(statement,'_for_update_arg',None) is not None and (lock_entity is None or statement.column_descriptions[0].get('entity') is lock_entity):
                            held.set();assert release.wait(10)
                        return result
                    db.scalar=pause
                else:ready.set()
                try:results[name]=(200,call(db))
                except HTTPException as exc:results[name]=(exc.status_code,None)
        except BaseException as exc:errors.append(exc)
    first=threading.Thread(target=actor,args=('first',first_call));second=threading.Thread(target=actor,args=('second',second_call))
    first.start()
    try:
        assert held.wait(8)
        second.start();assert ready.wait(8)
        assert pids['first']!=pids['second']
        observe_lock_wait(maker,pids['second'],pids['first'])
        assert 'second' not in results
    finally:
        release.set();first.join(22)
        if second.ident is not None:second.join(22)
    assert not first.is_alive() and not second.is_alive() and not errors
    return results


@pytest.mark.parametrize('different_question,different_learner',[(False,False),(True,False),(False,True)])
def test_concurrent_awards_are_once_only_without_lost_points(pg_sessions,different_question,different_learner):
    room,creator,learner,other,q,q2=seed(pg_sessions,different_question,different_learner)
    def submit(db,qid,uid):return challenges.submit_answer(room,qid,AnswerSubmission(answer='offline flag'),db.get(User,uid),db)
    results=interleave(pg_sessions,lambda db:submit(db,q,learner),lambda db:submit(db,q2,other))
    assert results['first'][0]==results['second'][0]==200
    assert results['first'][1].points_awarded==100
    assert results['second'][1].points_awarded==(75 if different_question else 100 if different_learner else 0)
    with pg_sessions() as db:
        solves=db.scalars(select(QuestionSolve)).all();assert len(solves)==(2 if different_question or different_learner else 1)
        p=db.scalar(select(RoomProgress).where(RoomProgress.learner_id==learner))
        assert p.points_earned==(175 if different_question else 100) and p.completed_at is not None
        assert p.attempt_count==(1 if different_learner else 2)


@pytest.mark.parametrize('change',[{'answer':'new value'},{'points':500}])
def test_submission_and_author_edit_serialize_and_preserve_award(pg_sessions,change):
    room,creator,learner,_,q,_=seed(pg_sessions)
    r=interleave(pg_sessions,
        lambda db:challenges.submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db),
        lambda db:challenges.update_question(room,q,QuestionUpdate(**change),db.get(User,creator),db))
    assert r['first'][0]==200 and r['second'][0]==409
    with pg_sessions() as db:
        assert db.get(ChallengeQuestion,q).points==100
        assert db.scalar(select(RoomProgress.points_earned))==100


def test_concurrent_rate_limit_cannot_overrun(pg_sessions):
    room,_,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        challenges.submit_answer(room,q,AnswerSubmission(answer='wrong'),db.get(User,learner),db)
        p=db.scalar(select(RoomProgress));p.attempt_count=19;db.commit()
    def submit(db):return challenges.submit_answer(room,q,AnswerSubmission(answer='wrong'),db.get(User,learner),db)
    r=interleave(pg_sessions,submit,submit)
    assert r['first'][0]==200 and r['second'][0]==429
    with pg_sessions() as db:assert db.scalar(select(RoomProgress.attempt_count))==20 and db.scalar(select(QuestionSolve.id)) is None


def test_postgres_unique_award_constraint_rejects_duplicate(pg_sessions):
    room,_,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        challenges.submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
        db.add(QuestionSolve(question_id=q,learner_id=learner,points_awarded=100))
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()
        assert db.scalar(select(RoomProgress.points_earned))==100


def test_real_postgres_additive_migration_and_active_order_index(pg_sessions):
    room,_,_,_,q,_=seed(pg_sessions)
    # Only the fixture's private schema is altered. Existing room data stays intact.
    with pg_sessions() as db:
        db.execute(text('DELETE FROM challenge_questions'))
        for table in reversed(TABLES):table.drop(db.connection())
        db.commit()
        assert migrate(db.connection());db.commit()
        assert not migrate(db.connection());db.commit()
        assert db.get(Room,room).name=='Concurrent challenge'
        assert inspect(db.connection()).has_table('question_solves')
        args=dict(room_id=room,question_text='Index test',answer_salt='a'*32,answer_digest='b'*64,points=100,display_order=1)
        db.add_all([ChallengeQuestion(**args),ChallengeQuestion(**args)])
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()
        db.add_all([ChallengeQuestion(**args,is_active=True),ChallengeQuestion(**args,is_active=False)])
        db.commit()
        assert len(db.scalars(select(ChallengeQuestion)).all())==2


def test_task_membership_migration_preserves_original_flags_and_awards(pg_sessions):
    from app.room_tasks_migration import migrate as task_migrate
    from app.models import RoomTask
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        before=(db.get(ChallengeQuestion,q).answer_digest,db.get(ChallengeQuestion,q).answer_salt,db.get(ChallengeQuestion,q).points)
        db.execute(text('ALTER TABLE challenge_questions DROP CONSTRAINT fk_challenge_question_task_room'));db.execute(text('ALTER TABLE challenge_questions DROP COLUMN task_id'));RoomTask.__table__.drop(db.connection());db.commit()
        assert task_migrate(db.connection());db.commit();assert not task_migrate(db.connection());db.commit()
        saved=db.get(ChallengeQuestion,q);assert saved.task_id is None and (saved.answer_digest,saved.answer_salt,saved.points)==before
        response=challenges.submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db);assert response.points_awarded==100


@pytest.mark.parametrize('deletion_first',[False,True])
def test_task_delete_and_award_serialize_without_losing_question_or_credit(pg_sessions,deletion_first):
    from app.room_tasks import create_task,TaskWrite
    from app.room_workspace import edit_workspace,publish_workspace,WorkspaceWrite,PublishWrite
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        r=db.get(Room,room);r.status='draft';r.room_type='THEORY';db.commit()
        task=create_task(room,TaskWrite(title='Read files',instructions='Real protected material'),db.get(User,creator),db)
        task_id=task.id;challenges.update_question(room,q,QuestionUpdate(task_id=task_id),db.get(User,creator),db)
        r.status='published';db.commit();digest=db.get(ChallengeQuestion,q).answer_digest
    def remove(db):
        user=db.get(User,creator)
        draft=edit_workspace(room,WorkspaceWrite(revision=0,action='task_remove',data={'id':task_id}),user,db)
        return publish_workspace(room,PublishWrite(revision=draft.revision),user,db)
    def solve(db):return challenges.submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
    r=interleave(pg_sessions,remove if deletion_first else solve,solve if deletion_first else remove)
    assert r['first'][0]==r['second'][0]==200
    with pg_sessions() as db:
        saved=db.get(ChallengeQuestion,q);assert saved.task_id is None and saved.answer_digest==digest
        assert db.scalar(select(RoomProgress.points_earned))==100 and db.scalar(select(QuestionSolve.question_id))==q


def test_task_assignment_cannot_race_deleted_task_or_cross_room(pg_sessions):
    from app.room_tasks import create_task,delete_task,TaskWrite
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        db.get(Room,room).status='draft';db.commit()
        task_id=create_task(room,TaskWrite(title='Section'),db.get(User,creator),db).id
    def remove(db):return delete_task(room,task_id,db.get(User,creator),db)
    def assign(db):return challenges.update_question(room,q,QuestionUpdate(task_id=task_id),db.get(User,creator),db)
    r=interleave(pg_sessions,remove,assign);assert r['first'][0]==200 and r['second'][0]==422
    with pg_sessions() as db:assert db.get(ChallengeQuestion,q).task_id is None


def test_education_migration_real_legacy_rooms_and_path_constraints(pg_sessions):
    from app.education_migration import migrate as education
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        # Remove only the additive columns/tables in this fixture's private schema.
        db.execute(text('DROP TABLE path_bookmarks'));db.execute(text('DROP TABLE learning_path_rooms'));db.execute(text('DROP TABLE learning_paths'))
        for name in ['room_type','objectives','prerequisites']:db.execute(text('ALTER TABLE rooms DROP COLUMN '+name))
        db.commit();assert education(db.connection());db.commit();assert not education(db.connection());db.commit()
        r=db.get(Room,room);assert r.room_type=='PRACTICAL' and r.status=='published' and r.creator_id==creator
        assert db.get(ChallengeQuestion,q).points==100
        from app.discovery_migration import migrate as current_discovery
        current_discovery(db.connection());db.commit()
        from app.operations_migration import migrate as current_operations
        current_operations(db.connection());db.commit()
        from app.models import LearningPath,LearningPathRoom
        p=LearningPath(creator_id=creator,title='Real PG journey',category='linux',difficulty='easy');db.add(p);db.flush()
        db.add(LearningPathRoom(path_id=p.id,room_id=room,display_order=1));db.commit()
        db.add(LearningPathRoom(path_id=p.id,room_id=999999,display_order=2))
        with pytest.raises(IntegrityError):db.commit()
        db.rollback();assert db.scalar(select(LearningPathRoom.room_id))==room


def test_theory_scoring_and_path_completion_use_same_real_postgres_ledger(pg_sessions):
    from fastapi import Response
    from app.models import LearningPath,LearningPathRoom
    from app.learning_paths import progress
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        db.get(Room,room).room_type='THEORY';p=LearningPath(creator_id=creator,title='Theory path',category='linux',difficulty='easy',status='published');db.add(p);db.flush();pid=p.id;db.add(LearningPathRoom(path_id=pid,room_id=room,display_order=1));db.commit()
    def submit(db):return challenges.submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
    result=interleave(pg_sessions,submit,submit)
    assert result['first'][1].points_awarded==100 and result['second'][1].points_awarded==0
    with pg_sessions() as db:
        p=progress(pid,Response(),db.get(User,learner),db);assert p.percent==100 and p.completed and p.next_room_id is None
        assert db.scalar(select(QuestionSolve.points_awarded))==100


def test_path_membership_updates_serialize_without_duplicate_order(pg_sessions):
    from app.learning_paths import create, membership, PathWrite, Membership
    from app.models import LearningPathRoom
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:pid=create(PathWrite(title='Concurrent path',category='linux'),db.get(User,creator),db).id
    # The same interleaving helper observes PostgreSQL's actual row-lock wait.
    # interleave's held SELECT FOR UPDATE gate applies to all row locks.
    r=interleave(pg_sessions,lambda db:membership(pid,Membership(room_ids=[room]),db.get(User,creator),db),lambda db:membership(pid,Membership(room_ids=[]),db.get(User,creator),db))
    assert r['first'][0]==r['second'][0]==200
    with pg_sessions() as db:assert db.scalar(select(LearningPathRoom.room_id)) is None


@pytest.mark.parametrize('second_add',[True,False])
def test_private_bookmark_duplicates_and_remove_serialize(pg_sessions,second_add):
    from app.bookmarks import change
    from app.models import RoomBookmark
    room,_,learner,_,_,_=seed(pg_sessions)
    r=interleave(pg_sessions,lambda db:change('rooms',room,db.get(User,learner),db,True),lambda db:change('rooms',room,db.get(User,learner),db,second_add))
    assert r['first'][0]==r['second'][0]==200
    with pg_sessions() as db:
        assert db.scalar(select(RoomBookmark.user_id))==(learner if second_add else None)
        assert db.scalar(select(QuestionSolve.id)) is None


def test_real_discovery_category_migration_preserves_original_and_bookmark_fks(pg_sessions):
    from app.discovery_migration import migrate as migration
    from app.models import RoomBookmark
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        db.execute(text("UPDATE rooms SET category='Uncertain study' WHERE id=:id"),dict(id=room))
        db.execute(text('DROP TABLE room_bookmarks'));db.execute(text('DROP TABLE path_bookmarks'))
        db.execute(text('ALTER TABLE rooms DROP COLUMN category_original'));db.execute(text('ALTER TABLE learning_paths DROP COLUMN category_original'));db.commit()
        before=db.get(ChallengeQuestion,q).answer_digest
        assert migration(db.connection());db.commit();assert not migration(db.connection());db.commit()
        db.expire_all();r=db.get(Room,room);assert r.category=='other' and r.category_original=='Uncertain study' and db.get(ChallengeQuestion,q).answer_digest==before
        db.add(RoomBookmark(user_id=learner,room_id=room));db.commit()
        db.add(RoomBookmark(user_id=learner,room_id=room))
        with pytest.raises(IntegrityError):db.commit()
        db.rollback();assert db.scalar(select(RoomBookmark.room_id))==room
        db.execute(text('DELETE FROM room_bookmarks'));db.delete(db.get(User,learner));db.commit()
        assert db.get(Room,room)


def test_representative_discovery_dataset_is_bounded_and_uses_indexes(pg_sessions,capsys):
    import time,json
    from app.discovery import room_page,path_page
    from app.discovery_migration import migrate as migration
    from app.models import LearningPath
    room,creator,learner,_,_,_=seed(pg_sessions)
    with pg_sessions() as db:
        migration(db.connection());db.commit()
        db.execute(Room.__table__.insert(),[dict(creator_id=creator,name=f'Linux lesson {i:05}',description='Command line fundamentals',category='linux' if i%2==0 else 'networking',difficulty='easy' if i%3==0 else 'medium',room_type='THEORY',status='published') for i in range(5000)]);db.commit()
        db.execute(LearningPath.__table__.insert(),[dict(creator_id=creator,title=f'Journey {i:04}',description='Command line fundamentals',difficulty='easy',category='linux',status='published') for i in range(1000)]);db.commit()
        db.execute(text('ANALYZE rooms'));db.execute(text('ANALYZE learning_paths'));db.commit()
        start=time.perf_counter()
        r=room_page(db,q='  COMMAND   line ',category='linux',difficulty='easy',sort='alphabetical',uid=learner)
        p=path_page(db,q='command line',category='linux',uid=learner)
        elapsed=time.perf_counter()-start
        assert 800<r.total<850 and len(r.items)==24 and p.total==1000 and len(p.items)==24
        assert elapsed<3, 'Bounded 5k room/1k path discovery exceeded the test budget'
        plan=db.scalar(text("EXPLAIN (FORMAT JSON) SELECT id FROM rooms WHERE status='published' AND category='linux' AND difficulty='easy' ORDER BY id LIMIT 24"))
        assert 'ix_rooms_discovery_filter' in json.dumps(plan) or 'Index' in json.dumps(plan)
        with capsys.disabled():print(f'PERFORMANCE rooms=5000 paths=1000 two_private_catalogs_ms={elapsed*1000:.1f} page_size=24 indexed_filter=true')


def test_concurrent_admin_changes_preserve_one_active_admin_and_revalidate_actor(pg_sessions):
    from app.models import AdminControl,AdminAudit
    from app.administration import change_user,AccountChange
    with pg_sessions() as db:
        a=User(username='admin_one',email='a@example.com',role='admin',password_hash='unused');b=User(username='admin_two',email='b@example.com',role='admin',password_hash='unused');db.add_all([a,b,AdminControl(id=1)]);db.commit();aid,bid=a.id,b.id
    def change(db,actor,target,name):
        data=AccountChange(role='creator',expected_role='admin',expected_suspended=False,confirm_username=name,reason='access-change')
        return change_user(target,data,db.get(User,actor),db)
    results=interleave(pg_sessions,lambda db:change(db,aid,bid,'admin_two'),lambda db:change(db,bid,aid,'admin_one'))
    assert results['first'][0]==200 and results['second'][0]==403
    with pg_sessions() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.role=='admin',User.suspended.is_(False)))==1
        assert [r.outcome for r in db.scalars(select(AdminAudit).order_by(AdminAudit.id))]==['success','denied']


def test_real_operations_migration_preserves_roles_and_reruns(pg_sessions):
    from app.operations_migration import migrate as operations
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        before=list(db.execute(text('SELECT id,role,username,token_version FROM users ORDER BY id')))
        db.execute(text('DROP TABLE admin_audit'));db.execute(text('DROP TABLE admin_control'));db.execute(text('ALTER TABLE users DROP COLUMN suspended'));db.execute(text('ALTER TABLE rooms DROP COLUMN moderation_hold'));db.execute(text('ALTER TABLE learning_paths DROP COLUMN moderation_hold'));db.commit()
        assert operations(db.connection());db.commit();assert not operations(db.connection());db.commit()
        assert list(db.execute(text('SELECT id,role,username,token_version FROM users ORDER BY id')))==before
        assert not db.scalar(select(User.suspended).limit(1))
        assert not db.get(Room,room).moderation_hold


def test_admin_and_creator_aggregations_bounded_on_thousands(pg_sessions,capsys):
    import time,json
    from fastapi import Response
    from app.creator_analytics import overview,rooms
    from app.administration import users
    room,creator,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        from app.operations_migration import migrate as operations
        operations(db.connection());db.commit()
        db.execute(Room.__table__.insert(),[dict(creator_id=creator,name=f'Analytics {i}',category='linux',difficulty='easy',room_type='THEORY',status='draft') for i in range(3000)])
        db.execute(User.__table__.insert(),[dict(username=f'member{i:05}',email=f'm{i}@example.com',role='learner',password_hash='unused') for i in range(5000)]);db.commit()
        db.execute(text('ANALYZE users'));db.execute(text('ANALYZE rooms'));db.commit()
        start=time.perf_counter();v=overview(Response(),db.get(User,creator),db);p=rooms(Response(),1,20,db.get(User,creator),db);a=users(Response(),'',1,20,db);elapsed=time.perf_counter()-start
        assert v.owned_rooms==3001 and len(p.items)==20 and len(a.items)==20
        plan=db.scalar(text('EXPLAIN (FORMAT JSON) SELECT id FROM users WHERE role=\'admin\' AND suspended=false'))
        assert 'ix_users_role_suspended' in json.dumps(plan)
        assert elapsed<4
        with capsys.disabled():print(f'OPERATIONS_PERFORMANCE rooms=3001 users=5002 overview_and_two_pages_ms={elapsed*1000:.1f}')


def test_first_admin_retains_creator_capability_real_postgres(pg_sessions):
    from app.operations_migration import migrate as operations
    from app.readiness_migration import migrate as readiness
    import importlib.util
    from pathlib import Path
    room,creator,learner,_,q,_=seed(pg_sessions)
    spec=importlib.util.spec_from_file_location('pg_bootstrap',Path(__file__).parent/'deploy/cloud/bootstrap_admin.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with pg_sessions() as db:
        operations(db.connection());db.commit()
        before=list(db.execute(text('SELECT id,role,password_hash FROM users ORDER BY id')))
        db.execute(text('ALTER TABLE users DROP COLUMN creator_access'));db.commit()
        assert readiness(db.connection());db.commit();assert not readiness(db.connection());db.commit()
        assert list(db.execute(text('SELECT id,role,password_hash FROM users ORDER BY id')))==before
        u=db.get(User,creator);name=u.username
        module.bootstrap(db,u.id,name,'PROMOTE '+str(u.id)+' '+name)
        assert u.role=='admin' and u.creator_access and u.token_version==1
        assert db.get(User,learner).role=='learner' and not db.get(User,learner).creator_access
        assert db.get(Room,room).creator_id==creator
