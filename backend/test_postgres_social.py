"""Actual row-lock races and migration checks in an owned disposable PostgreSQL."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned PostgreSQL concurrency runner',allow_module_level=True)
from types import SimpleNamespace
from fastapi import Response
from sqlalchemy import select,func,text
from app.models import User,UserFollow,UserBlock
from app.social import follow,block
from app.social_migration import migrate
from test_postgres_lab_concurrency import pg_sessions
from test_postgres_challenges import seed,interleave

def request(db,actor,target,action='follow',enabled=True):
    fn=follow if action=='follow' else block
    return fn(db.get(User,target).username,Response(),SimpleNamespace(method='PUT' if enabled else 'DELETE'),db.get(User,actor),db)

@pytest.mark.parametrize('op',['duplicate','opposite','unfollow','block-first','follow-first'])
def test_relationship_writes_serialize_with_blocks_and_duplicate_requests(pg_sessions,op):
    _,a,b,_,_,_=seed(pg_sessions)
    if op=='duplicate':first=lambda db:request(db,a,b);second=lambda db:request(db,a,b)
    elif op=='opposite':first=lambda db:request(db,a,b);second=lambda db:request(db,b,a)
    elif op=='unfollow':first=lambda db:request(db,a,b);second=lambda db:request(db,a,b,enabled=False)
    elif op=='block-first':first=lambda db:request(db,a,b,'block');second=lambda db:request(db,b,a)
    else:first=lambda db:request(db,a,b);second=lambda db:request(db,b,a,'block')
    results=interleave(pg_sessions,first,second)
    assert results['first'][0]==200 and results['second'][0]==(403 if op=='block-first' else 200)
    with pg_sessions() as db:
        assert db.scalar(select(func.count()).select_from(UserFollow))==({'duplicate':1,'opposite':2}.get(op,0))
        assert db.scalar(select(func.count()).select_from(UserBlock))==int('first' in op)


def test_real_postgres_migration_rerun_and_cascade_preserves_accounts(pg_sessions):
    _,a,b,_,_,_=seed(pg_sessions)
    with pg_sessions() as db:
        db.execute(text('DROP TABLE user_follows'));db.execute(text('DROP TABLE user_blocks'));db.commit()
        assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        request(db,a,b)
        assert db.scalar(select(func.count()).select_from(UserFollow))==1
        db.execute(text('DELETE FROM users WHERE id=:id'),{'id':b});db.commit()
        assert db.scalar(select(func.count()).select_from(UserFollow))==0
        assert db.get(User,a) is not None


@pytest.mark.parametrize('role',['creator','admin'])
def test_creator_learning_concurrent_solves_award_once(pg_sessions,role):
    from app.challenges import submit_answer
    from app.schemas import AnswerSubmission
    from app.models import QuestionSolve,RoomProgress
    room,owner,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:db.get(User,learner).role=role;db.commit()
    results=interleave(pg_sessions,lambda db:submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db),lambda db:submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db))
    assert results['first'][1].points_awarded==100 and results['second'][1].points_awarded==0
    with pg_sessions() as db:
        assert db.scalar(select(func.count()).select_from(QuestionSolve))==1
        assert db.scalar(select(RoomProgress.points_earned))==100


def test_indexed_connection_pagination_on_two_thousand_users(pg_sessions):
    import time
    from app.social import connections
    _,a,b,_,_,_=seed(pg_sessions)
    with pg_sessions() as db:
        users=[User(username=f'social_perf_{i:04}',email=f'social-{i}@example.com',password_hash='unused',role='learner') for i in range(2000)]
        db.add_all(users);db.flush()
        db.add_all([UserFollow(follower_id=u.id,followed_id=a) for u in users]);db.commit()
        db.execute(text('ANALYZE user_follows'));db.execute(text('ANALYZE users'));db.commit()
        start=__import__('time').perf_counter()
        result=connections(db.get(User,a).username,'followers',Response(),1,20,db)
        elapsed=(time.perf_counter()-start)*1000
        assert result.total==2000 and len(result.items)==20 and elapsed<1000
        plan='\n'.join(db.scalars(text('EXPLAIN (ANALYZE, BUFFERS) SELECT follower_id FROM user_follows WHERE followed_id=:id ORDER BY created_at DESC,follower_id LIMIT 20'),{'id':a}))
        assert 'ix_follow_incoming' in plan and 'Execution Time:' in plan
        import sys
        print(f'SOCIAL_PERFORMANCE users=2000 counts_and_20_summaries_ms={elapsed:.1f} incoming_index=true',file=sys.__stdout__)
