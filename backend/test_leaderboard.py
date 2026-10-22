"""Read-only ranking accuracy, privacy and UTC windows on owned data."""
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import asyncio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select,event,text,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
from app.database import Base,get_db
from app.main import app
from app.models import User,Room,ChallengeQuestion,QuestionSolve,RoomProgress
from app import leaderboard as ranking
from app.leaderboard_migration import migrate,INDEXES
from app.security import create_access_token
from app.config import settings
from app.login_limit import LeaderboardRateLimitMiddleware
NOW=datetime(2026,10,13,12,tzinfo=timezone.utc)
@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings,'reconciliation_enabled',False);monkeypatch.setattr(ranking,'utc_now',lambda:NOW);monkeypatch.setattr(settings,'production',False)
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    event.listen(engine,'connect',lambda c,r:c.execute('PRAGMA foreign_keys=ON'))
    Base.metadata.create_all(engine);maker=sessionmaker(bind=engine)
    with maker() as db:
        users=[User(username=n,email=n+'@example.com',role=role,password_hash='private-hash',bio='Private bio',avatar_version='a'*32) for n,role in [('first','learner'),('second','learner'),('third','learner'),('fourth','learner'),('creator','creator'),('admin','admin'),('empty','learner')]]
        db.add_all(users);db.flush();ids={u.username:u.id for u in users}
        for i in range(1,7):
            room=Room(id=i,creator_id=ids['creator'],name='Hidden room '+str(i),status='draft',category='Linux',difficulty='easy');db.add(room);db.flush()
            for j in range(1,3):db.add(ChallengeQuestion(id=(i-1)*2+j,room_id=i,question_text='Secret task',answer_salt='s'*32,answer_digest='d'*64,points=25,display_order=j))
        db.commit()
    old=dict(app.dependency_overrides)
    def dependency():
        with maker() as db:yield db
    app.dependency_overrides[get_db]=dependency
    with TestClient(app) as client:
        yield SimpleNamespace(client=client,maker=maker,engine=engine,ids=ids,header=lambda name:{'Authorization':'Bearer '+create_access_token(ids[name],'learner' if name not in ('creator','admin') else name)})
    app.dependency_overrides.clear();app.dependency_overrides.update(old);engine.dispose()
def award(env,name,q,points=25,when=None):
    with env.maker() as db:db.add(QuestionSolve(learner_id=env.ids[name],question_id=q,points_awarded=points,solved_at=(when or NOW).replace(tzinfo=None)));db.commit()
def complete(env,name,room=1,when=None):
    with env.maker() as db:db.add(RoomProgress(learner_id=env.ids[name],room_id=room,points_earned=999999,completed_at=(when or NOW).replace(tzinfo=None)));db.commit()
def board(env,period='all-time',**params):return env.client.get('/api/public/leaderboard',params={'period':period,**params})

def test_empty_guest_and_unranked_own_summary(env):
    r=board(env);assert r.status_code==200 and r.json()['items']==[] and r.json()['total']==0 and r.json()['leaders']==[]
    assert env.client.get('/api/leaderboard/me').status_code in (401,403)
    own=env.client.get('/api/leaderboard/me',headers=env.header('empty')).json();assert own['rank'] is None and own['points']==0 and own['reason']=='no-points'

def test_legitimate_ledger_excludes_visits_cached_scores_and_non_learners(env):
    complete(env,'empty');award(env,'first',1,75);award(env,'second',1,25);award(env,'creator',1,1000);award(env,'admin',1,1000)
    data=board(env).json();assert [(r['username'],r['points']) for r in data['items']]==[('creator',1000),('admin',1000),('first',75),('second',25)]
    assert all(set(r)=={'username','display_name','avatar_style','avatar_color','avatar_url','rank','points','rooms_completed'} for r in data['items'])
    assert 'private-hash' not in str(data) and 'Private bio' not in str(data) and '@example' not in str(data)
    own=env.client.get('/api/leaderboard/me',headers=env.header('creator')).json();assert own['rank']==1 and own['points']==1000 and own['reason']=='ranked'

def test_ties_use_completion_then_earliest_last_award_then_stable_id(env):
    for name in ['first','second','third','fourth']:award(env,name,1,when=NOW-timedelta(hours=1))
    complete(env,'third');award(env,'fourth',2,1,when=NOW);award(env,'first',2,1,when=NOW-timedelta(minutes=30));award(env,'second',2,1,when=NOW-timedelta(minutes=30));award(env,'third',2,1,when=NOW)
    values=[board(env).json() for _ in range(2)];assert values[0]==values[1]
    assert [r['username'] for r in values[0]['items']]==['third','first','second','fourth']
    assert [r['rank'] for r in values[0]['items']]==[1,2,3,4]
    with env.maker() as db:db.get(User,env.ids['first']).username='renamed';db.commit()
    assert [r['username'] for r in board(env).json()['items']]==['third','renamed','second','fourth']

@pytest.mark.parametrize('period,start,end',[('weekly',datetime(2026,10,12,tzinfo=timezone.utc),datetime(2026,10,19,tzinfo=timezone.utc)),('monthly',datetime(2026,10,1,tzinfo=timezone.utc),datetime(2026,11,1,tzinfo=timezone.utc))])
def test_utc_inclusive_start_exclusive_end_and_period_points(env,period,start,end):
    award(env,'first',1,when=start-timedelta(microseconds=1));award(env,'first',2,when=start);award(env,'first',3,when=end-timedelta(microseconds=1));award(env,'first',4,when=end)
    data=board(env,period).json();assert data['items'][0]['points']==50
    assert datetime.fromisoformat(data['starts_at'])==start and datetime.fromisoformat(data['ends_at'])==end
    assert board(env).json()['items'][0]['points']==100

@pytest.mark.parametrize('date,period,start,end',[(datetime(2027,1,1,tzinfo=timezone.utc),'weekly',datetime(2026,12,28,tzinfo=timezone.utc),datetime(2027,1,4,tzinfo=timezone.utc)),(datetime(2026,12,31,tzinfo=timezone.utc),'monthly',datetime(2026,12,1,tzinfo=timezone.utc),datetime(2027,1,1,tzinfo=timezone.utc)),(datetime(2028,2,29,tzinfo=timezone.utc),'monthly',datetime(2028,2,1,tzinfo=timezone.utc),datetime(2028,3,1,tzinfo=timezone.utc))])
def test_year_and_leap_boundaries(date,period,start,end):assert ranking.boundaries(period,date)==(start,end)

def test_completion_tie_counts_selected_period_and_requires_real_solves(env):
    award(env,'first',1);award(env,'second',1);complete(env,'second',when=NOW-timedelta(days=20));complete(env,'first',room=2)
    assert board(env,'weekly').json()['items'][0]['username']=='first' # stable ID; neither has current qualifying completion
    assert all(r['rooms_completed']==0 for r in board(env,'weekly').json()['items'])
    assert board(env).json()['items'][0]['username']=='second'

def test_public_profile_has_only_earned_safe_fields_and_matches_private_statistics(env):
    award(env,'first',1,75);complete(env,'first')
    response=env.client.get('/api/public/learners/FIRST');assert response.status_code==200
    value=response.json();assert set(value)=={'username','display_name','avatar_style','avatar_color','avatar_url','bio','points','rooms_completed','questions_solved','achievements'}
    assert value['points']==75 and value['rooms_completed']==1 and value['questions_solved']==1
    assert [b['id'] for b in value['achievements']]==['first-answer','first-room']
    assert all(set(b)=={'id','name','description','earned_at'} for b in value['achievements'])
    own=env.client.get('/api/users/me/learning',headers=env.header('first')).json()['stats'];assert own['points']==value['points'] and own['rooms_completed']==value['rooms_completed'] and own['questions_solved']==value['questions_solved']
    assert not any(s in str(value) for s in ['email','Private bio','avatar_version','password','token','Hidden room','Secret task','vpn','learner_id'])

@pytest.mark.parametrize('username',['unknown','invalid name','x'*51])
def test_unranked_private_and_unknown_accounts_have_same_unavailable_profile(env,username):
    r=env.client.get('/api/public/learners/'+username);assert r.status_code==404 and r.json()=={'detail':'Learner profile unavailable'}

def test_soft_removed_questions_keep_credit_and_deleted_solve_removes_credit(env):
    award(env,'first',1)
    with env.maker() as db:db.get(ChallengeQuestion,1).is_active=False;db.commit()
    assert board(env).json()['items'][0]['points']==25
    with env.maker() as db:db.delete(db.scalar(select(QuestionSolve)));db.commit()
    assert board(env).json()['total']==0 and env.client.get('/api/public/learners/first').json()['points']==0

def test_deleted_accounts_never_appear(env):
    award(env,'first',1)
    with env.maker() as db:db.delete(db.get(User,env.ids['first']));db.commit()
    assert board(env).json()['total']==0

def test_duplicate_award_constraint_and_reads_never_change_scores(env):
    award(env,'first',1)
    with env.maker() as db:
        db.add(QuestionSolve(learner_id=env.ids['first'],question_id=1,points_awarded=25))
        with pytest.raises(IntegrityError):db.commit()
        db.rollback()
    for _ in range(3):assert board(env).json()['items'][0]['points']==25
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(QuestionSolve))==1

def test_pagination_top_three_and_current_rank_outside_page(env):
    for i,name in enumerate(['first','second','third','fourth']):award(env,name,1,100-i)
    page=board(env,page=2,page_size=2).json();assert [r['rank'] for r in page['items']]==[3,4] and [r['rank'] for r in page['leaders']]==[1,2,3] and page['total']==4
    own=env.client.get('/api/leaderboard/me',headers=env.header('fourth')).json();assert own['rank']==4 and own['points']==97
    assert board(env,page=10).json()['items']==[] and board(env,page=10).json()['total']==4

@pytest.mark.parametrize('params',[{'period':'invalid'},{'page':0},{'page':10001},{'page_size':51},{'page_size':0},{'page':'nan'}])
def test_invalid_parameters(env,params):assert env.client.get('/api/public/leaderboard',params=params).status_code==422

def test_private_beta_disabled_users_are_not_publicly_ranked(env,monkeypatch):
    award(env,'first',1);award(env,'second',1);monkeypatch.setattr(settings,'production',True);monkeypatch.setattr(settings,'private_beta_enabled',True);monkeypatch.setattr(settings,'private_beta_user_ids',[env.ids['first']])
    assert [r['username'] for r in board(env).json()['items']]==['first'] and env.client.get('/api/public/learners/second').status_code==404

def test_public_api_is_read_only_and_protected_actions_remain_protected(env):
    for method in ['post','put','patch','delete']:assert env.client.request(method,'/api/public/leaderboard',json={}).status_code==405
    for path in ['/api/vpn/profile','/api/users/me/activity','/api/rooms/1/builder']:assert env.client.get(path).status_code in (401,403)

def test_additive_indexes_idempotent_and_no_data_rewrite(env):
    award(env,'first',1)
    with env.engine.begin() as c:
        for _,name,_,_ in INDEXES:c.execute(text('DROP INDEX '+name))
        assert migrate(c);assert not migrate(c)
    assert board(env).json()['items'][0]['points']==25

def test_thousands_of_users_use_one_bounded_ranking_statement(env):
    with env.maker() as db:
        users=[User(username='scale'+str(i),email='scale'+str(i)+'@example.com',role='learner',password_hash='unused') for i in range(2500)];db.add_all(users);db.flush()
        db.add_all([QuestionSolve(learner_id=u.id,question_id=1,points_awarded=(i%1000)+1,solved_at=NOW.replace(tzinfo=None)) for i,u in enumerate(users)]);db.commit()
    statements=[]
    def track(c,cu,s,p,context,many):statements.append(s)
    event.listen(env.engine,'before_cursor_execute',track)
    try:r=board(env,page=50).json()
    finally:event.remove(env.engine,'before_cursor_execute',track)
    assert r['total']==2500 and len(r['items'])==20 and len(r['leaders'])==3 and len(statements)==1
