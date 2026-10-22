"""Guest disclosure/authorization tests against an isolated database; no AWS."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,event,select,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.config import settings
from app.database import Base,get_db
from app.main import app
from app.models import User,Room,ChallengeQuestion,LabInstance,BuilderEnvironment
from app.security import hash_password,create_access_token
from app.login_limit import PublicRoomRateLimitMiddleware

@pytest.fixture
def public_env(monkeypatch):
    monkeypatch.setattr(settings,'production',False)
    monkeypatch.setattr(settings,'private_beta_enabled',False)
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
    event.listen(engine,'connect',lambda c,r:c.execute('PRAGMA foreign_keys=ON'))
    Base.metadata.create_all(engine);maker=sessionmaker(bind=engine)
    with maker() as db:
        owner=User(username='publiccreator',email='creator@example.com',role='creator',password_hash=hash_password('synthetic-public-test-passphrase'))
        learner=User(username='publiclearner',email='learner@example.com',role='learner',password_hash=owner.password_hash)
        db.add_all([owner,learner]);db.flush();owner_id=owner.id;learner_id=learner.id
        for i,status in enumerate(['published','published','draft','pending_review','rejected','private','archived']):
            db.add(Room(id=i+1,creator_id=owner.id,name=['Linux Beginner','Web 100% practical'][i] if i<2 else 'Hidden '+status,description='Inspect files and processes' if i==0 else 'Browser service',category='linux' if i==0 else 'web-security',difficulty='easy' if i==0 else 'hard',status=status))
        db.flush();db.add(ChallengeQuestion(room_id=1,question_text='Protected task',hint='Private hint',answer_salt='x'*32,answer_digest='a'*64,points=25,display_order=1));db.commit()
    def dependency():
        with maker() as db:yield db
    old=dict(app.dependency_overrides);app.dependency_overrides[get_db]=dependency
    client=TestClient(app)
    yield client,maker,owner_id,learner_id
    client.close();app.dependency_overrides.clear();app.dependency_overrides.update(old);engine.dispose()


def test_public_allowlist_published_only_and_no_protected_joins(public_env):
    c,_,_,_=public_env
    result=c.get('/api/public/rooms').json()
    assert result['total']==2 and result['categories']==list(__import__('app.categories',fromlist=['SLUGS']).SLUGS)
    assert [r['id'] for r in result['items']]==[2,1]
    for room in result['items']:
        assert set(room)=={'id','name','description','category','difficulty','room_type','objectives','prerequisites','points_total','categories'}
    detail=c.get('/api/public/rooms/1');assert detail.status_code==200
    assert detail.headers['cache-control']=='no-store'
    assert detail.json()==result['items'][1]
    for forbidden in ['creator_id','status','ami_id','private_ip','answer_digest','Private hint','Protected task','password_hash','vpn','snapshot']:
        assert forbidden not in detail.text

@pytest.mark.parametrize('room_id',[3,4,5,6,7,888])
def test_nonpublic_and_missing_rooms_are_indistinguishable(public_env,room_id):
    r=public_env[0].get('/api/public/rooms/'+str(room_id));assert r.status_code==404 and r.json()=={'detail':'Room not found'}

@pytest.mark.parametrize('params,ids',[({'q':'linux'},[1]),({'q':'PROCESSES'},[1]),({'q':'web'},[2]),({'q':'%'},[2]),({'q':'_'},[]),({'category':'linux'},[1]),({'difficulty':'hard'},[2]),({'category':'web-security','difficulty':'easy'},[]),({'q':"' OR 1=1 --"},[])])
def test_search_filters_are_case_insensitive_literal_and_bound(public_env,params,ids):
    r=public_env[0].get('/api/public/rooms',params=params);assert r.status_code==200
    assert [x['id'] for x in r.json()['items']]==ids

@pytest.mark.parametrize('params',[{'q':'a'*101},{'category':'a'*51},{'difficulty':'invalid'},{'page':0},{'page':1001},{'page_size':0},{'page_size':49},{'page':'NaN'}])
def test_public_validation_limits(public_env,params):
    assert public_env[0].get('/api/public/rooms',params=params).status_code==422

def test_pagination_empty_and_unpublish_immediately_removes_visibility(public_env):
    c,m,_,_=public_env
    assert [r['id'] for r in c.get('/api/public/rooms?page_size=1&page=2').json()['items']]==[1]
    with m() as db:
        for r in db.scalars(select(Room).where(Room.status=='published')):r.status='draft'
        db.commit()
    assert c.get('/api/public/rooms').json()==dict(items=[],total=0,page=1,page_size=24,categories=list(__import__('app.categories',fromlist=['SLUGS']).SLUGS))
    assert c.get('/api/public/rooms/1').status_code==404

@pytest.mark.parametrize('method,path,body',[
 ('GET','/api/rooms',None),('GET','/api/rooms/1',None),
 ('POST','/api/rooms/1/lab',{'ttl_hours':1}),('POST','/api/rooms/1/lab/stop',{}),
 ('POST','/api/rooms/1/lab/extend',{'expected_extensions_used':0}),
 ('GET','/api/vpn/profile',None),('GET','/api/rooms/1/questions',None),
 ('POST','/api/rooms/1/questions/1/submit',{'answer':'synthetic'}),('GET','/api/rooms/1/progress',None),
 ('GET','/api/rooms/1/builder',None),('POST','/api/rooms/1/builder',{}),
 ('GET','/api/rooms/1/templates',None),('POST','/api/rooms/1/templates/1/lab',{}),
 ('POST','/api/admin/invitations',{'email':'synthetic@example.com'}),('PATCH','/api/users/me',{'display_name':'Unauthorized'}),
 ('POST','/api/rooms',{'name':'Unauthorized','category':'Linux','difficulty':'easy'}),
])
def test_guests_cannot_interact_or_access_private_apis(public_env,method,path,body):
    c,m,_,_=public_env
    r=c.request(method,path,json=body)
    # Actual endpoint absence is separately checked: no 404 can satisfy this test.
    assert r.status_code in (401,403),path
    with m() as db:
        assert db.scalar(select(func.count()).select_from(LabInstance))==0
        assert db.scalar(select(func.count()).select_from(BuilderEnvironment))==0

def test_authenticated_room_permissions_unchanged(public_env):
    c,_,owner,learner=public_env
    headers=lambda uid,role:{'Authorization':'Bearer '+create_access_token(user_id=uid,role=role)}
    assert len(c.get('/api/rooms',headers=headers(owner,'creator')).json())==7
    assert c.get('/api/rooms/3',headers=headers(owner,'creator')).status_code==200
    assert len(c.get('/api/rooms',headers=headers(learner,'learner')).json())==2
    assert c.get('/api/rooms/3',headers=headers(learner,'learner')).status_code==404
    assert c.get('/api/rooms/1/questions',headers=headers(learner,'learner')).status_code==200

def test_public_rate_budget_separate_bounded_and_recoverable():
    clock=[0]
    async def app(scope,receive,send):await send({'type':'http.response.start','status':200,'headers':[]})
    middleware=PublicRoomRateLimitMiddleware(app,enabled=True,limit=2,window=60,clock=lambda:clock[0])
    async def request(path):
        statuses=[]
        async def send(message):
            if message['type']=='http.response.start':statuses.append(message['status'])
        await middleware({'type':'http','method':'GET','path':path,'client':('synthetic-client',1)},None,send)
        return statuses[0]
    async def run():
        assert await request('/api/public/rooms')==200
        assert await request('/api/auth/me')==200
        assert await request('/api/public/rooms/1')==200
        assert await request('/api/public/rooms/1')==429
        clock[0]=61
        assert await request('/api/public/rooms')==200
    asyncio.run(run())
