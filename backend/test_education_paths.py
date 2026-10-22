"""Theory and path integration uses only isolated data; cloud calls are forbidden."""
import pytest
from sqlalchemy import select,func,text,create_engine,event,inspect
from app.models import Room,LearningPathRoom,RoomProgress,QuestionSolve,User,LearningPath
from app.education_migration import migrate
from test_room_tasks import env

def newroom(e,kind='THEORY',name='Browser lesson',owner='owner'):
    return e.client.post('/api/rooms',headers=e.header(owner),json={'name':name,'category':'linux','difficulty':'easy','room_type':kind,'objectives':'Inspect permissions','prerequisites':'None'})
def path(e,owner='owner'):
    return e.client.post('/api/paths',headers=e.header(owner),json={'title':'Linux journey','category':'linux','description':'Learn safely','objectives':'Read then practice'})
def publish(e,room):return e.client.post(f'/api/rooms/{room}/publish',headers=e.header('owner'))
def membership(e,p,ids,owner='owner'):return e.client.put(f'/api/paths/{p}/rooms',headers=e.header(owner),json={'room_ids':ids})
def publishpath(e,p):return e.client.post(f'/api/paths/{p}/publish',headers=e.header('owner'))

def test_existing_rooms_practical_type_immutable_and_publish_gate(env):
    assert env.client.get('/api/rooms/1',headers=env.header('owner')).json()['room_type']=='PRACTICAL'
    assert env.client.patch('/api/rooms/1',headers=env.header('owner'),json={'room_type':'THEORY'}).status_code==422
    assert publish(env,1).status_code==409
    with env.maker() as db:assert db.get(Room,1).room_type=='PRACTICAL' and db.get(Room,1).status=='draft'

@pytest.mark.parametrize('kind',['unknown','theory','',None])
def test_invalid_room_types(env,kind):assert newroom(env,kind).status_code==422

def test_theory_end_to_end_reuses_confidential_grading_progress_and_points(env,monkeypatch):
    import boto3
    monkeypatch.setattr(boto3,'client',lambda *a,**k:pytest.fail('Theory workflow attempted AWS'))
    r=newroom(env);assert r.status_code==201;rid=r.json()['id']
    assert env.client.patch(f'/api/rooms/{rid}',headers=env.header('owner'),json={'description':'Actual lesson','objectives':'Updated objective'}).status_code==200
    t=env.client.post(f'/api/rooms/{rid}/tasks',headers=env.header('owner'),json={'title':'Read first','instructions':'# Material\n**Read** safely. <script>inert</script>'}).json()
    q=env.client.post(f'/api/rooms/{rid}/questions',headers=env.header('owner'),json={'question_text':'What did you learn?','answer':'synthetic-private-value','points':75,'task_id':t['id']}).json()
    assert publish(env,rid).status_code==200
    public=env.client.get(f'/api/public/rooms/{rid}');assert public.status_code==200 and public.json()['room_type']=='THEORY'
    assert not any(v in public.text for v in ['synthetic-private-value','Read first','inert','answer_digest','ami_id'])
    assert env.client.get(f'/api/rooms/{rid}/tasks').status_code in (401,403)
    url=f"/api/rooms/{rid}/questions/{q['id']}/submit"
    assert env.client.post(url,headers=env.header('learner'),json={'answer':'incorrect'}).json()['correct'] is False
    first=env.client.post(url,headers=env.header('learner'),json={'answer':'synthetic-private-value'});assert first.status_code==200 and first.json()['points_awarded']==75
    assert env.client.post(url,headers=env.header('learner'),json={'answer':'synthetic-private-value'}).json()['points_awarded']==0
    p=env.client.get(f'/api/rooms/{rid}/progress',headers=env.header('learner')).json();assert p['completed'] and p['points_earned']==75
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(QuestionSolve))==1 and db.scalar(select(RoomProgress.points_earned))==75

@pytest.mark.parametrize('suffix,method,body',[
    ('builder','post',{}),('builder','get',None),('builder/stop','post',None),
    ('templates','get',None),('templates','post',{}),('templates/1/publish','post',None),
    ('templates/1/lab','post',{}),('test-lab','get',None),('test-lab/stop','post',None),
    ('lab','post',{}),('lab','get',None),('lab/stop','post',None),('lab/extend','post',{})])
def test_theory_machine_routes_rejected_before_side_effects(env,monkeypatch,suffix,method,body):
    import boto3
    monkeypatch.setattr(boto3,'client',lambda *a,**k:pytest.fail('Machine API reached AWS'))
    rid=newroom(env).json()['id'];publish(env,rid)
    actor='learner' if suffix.startswith('lab') else 'owner'
    kwargs={'headers':env.header(actor)}
    if body is not None:kwargs['json']=body
    response=getattr(env.client,method)(f'/api/rooms/{rid}/{suffix}',**kwargs)
    assert response.status_code==409,response.text


def test_zero_question_theory_is_reading_only_not_completed(env):
    rid=newroom(env).json()['id'];assert publish(env,rid).status_code==200
    p=env.client.get(f'/api/rooms/{rid}/progress',headers=env.header('learner')).json();assert not p['completed'] and p['points_earned']==0
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(RoomProgress))==0

def test_path_crud_order_ownership_publication_and_safe_visibility(env):
    theory=newroom(env).json()['id'];practical=1
    p=path(env);assert p.status_code==201;pid=p.json()['id']
    assert membership(env,pid,[practical,theory]).status_code==200
    assert env.client.get(f'/api/public/paths/{pid}').status_code==404
    assert publishpath(env,pid).status_code==409
    publish(env,theory)
    with env.maker() as db:db.get(Room,practical).status='published';db.commit()
    assert membership(env,pid,[theory,practical]).status_code==200
    assert membership(env,pid,[theory,theory]).status_code==422
    assert membership(env,pid,[theory],owner='other').status_code==404
    assert env.client.get(f'/api/paths/{pid}',headers=env.header('learner')).status_code==403
    assert publishpath(env,pid).status_code==200
    detail=env.client.get(f'/api/public/paths/{pid}').json();assert [r['id'] for r in detail['rooms']]==[theory,practical]
    assert [r['room_type'] for r in detail['rooms']]==['THEORY','PRACTICAL']
    assert membership(env,pid,[theory]).status_code==409
    assert env.client.delete(f'/api/paths/{pid}',headers=env.header('owner')).status_code==409
    assert env.client.post(f'/api/paths/{pid}/unpublish',headers=env.header('owner')).status_code==200
    assert env.client.delete(f'/api/paths/{pid}',headers=env.header('owner')).status_code==204
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(LearningPathRoom))==0 and db.get(Room,theory)

def test_cross_creator_room_references_unpublish_and_delete(env):
    p=path(env).json()['id'];assert membership(env,p,[2]).status_code==422
    with env.maker() as db:db.get(Room,2).status='published';db.commit()
    assert membership(env,p,[2]).status_code==200 and publishpath(env,p).status_code==200
    assert env.client.get(f'/api/public/paths/{p}').json()['room_count']==1
    with env.maker() as db:db.get(Room,2).status='draft';db.commit()
    assert env.client.get(f'/api/public/paths/{p}').json()['rooms']==[]
    assert env.client.get(f'/api/paths/{p}',headers=env.header('owner')).json()['rooms']==[]
    assert env.client.get('/api/paths/eligible-rooms',headers=env.header('owner')).json()['items'][0]['id']!=2
    with env.maker() as db:db.delete(db.get(Room,2));db.commit()
    assert env.client.get(f'/api/public/paths/{p}').json()['room_count']==0

def test_path_progress_continue_and_room_completion_changes(env):
    a=newroom(env).json()['id'];b=newroom(env,name='Second lesson').json()['id'];publish(env,a);publish(env,b)
    pid=path(env).json()['id'];membership(env,pid,[a,b]);publishpath(env,pid)
    url=f'/api/paths/{pid}/progress'
    assert env.client.get(url).status_code in (401,403)
    assert env.client.get(url,headers=env.header('owner')).status_code==200
    p=env.client.get(url,headers=env.header('learner')).json();assert p['percent']==0 and p['next_room_id']==a and not p['completed']
    from datetime import datetime
    with env.maker() as db:db.add(RoomProgress(room_id=a,learner_id=env.ids['learner'],completed_at=datetime.utcnow()));db.commit()
    p=env.client.get(url,headers=env.header('learner')).json();assert p['percent']==50 and p['next_room_id']==b
    with env.maker() as db:db.add(RoomProgress(room_id=b,learner_id=env.ids['learner'],completed_at=datetime.utcnow()));db.commit()
    p=env.client.get(url,headers=env.header('learner')).json();assert p['completed'] and p['next_room_id'] is None
    with env.maker() as db:db.get(Room,b).status='draft';db.commit()
    p=env.client.get(url,headers=env.header('learner')).json();assert p['rooms_total']==1 and p['completed_room_ids']==[a]

@pytest.mark.parametrize('query',['page=0','page_size=49','difficulty=bad','q='+'x'*101,'page=1001'])
def test_path_discovery_validation(env,query):assert env.client.get('/api/public/paths?'+query).status_code==422

def test_public_path_search_pagination_limits_and_query_count(env):
    a=newroom(env).json()['id'];publish(env,a)
    for i in range(3):
        pid=path(env).json()['id'];membership(env,pid,[a]);publishpath(env,pid)
    draft=path(env).json()['id'];queries=[]
    event.listen(env.engine,'before_cursor_execute',lambda *args:queries.append(args[2]))
    r=env.client.get('/api/public/paths?q=linux&category=linux&difficulty=easy&page_size=2&page=2');assert r.status_code==200 and r.json()['total']==3 and len(r.json()['items'])==1
    assert len(queries)<=4
    assert env.client.get('/api/public/paths?q=%25').json()['total']==0
    assert env.client.get(f'/api/public/paths/{draft}').status_code==404
    assert not any(k in r.text for k in ['creator_id','email','private_ip','ami_id','answer_digest','instructions'])


def test_atomic_path_edit_rejects_invalid_membership_without_partial_metadata(env):
    p=path(env).json()['id'];r=env.client.patch(f'/api/paths/{p}',headers=env.header('owner'),json={'title':'Should roll back','category':'linux','room_ids':[2]});assert r.status_code==422
    assert env.client.get(f'/api/paths/{p}',headers=env.header('owner')).json()['title']=='Linux journey'


def test_legacy_migration_defaults_and_rerun_preserve_data():
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY)'));c.execute(text('CREATE TABLE rooms(id INTEGER PRIMARY KEY, name VARCHAR(100))'));c.execute(text("INSERT INTO rooms(id,name) VALUES(1,'Retain')"))
        assert migrate(c);assert not migrate(c)
        row=c.execute(text('SELECT name,room_type,objectives,prerequisites FROM rooms')).one();assert tuple(row)==('Retain','PRACTICAL','','')
        assert inspect(c).has_table('learning_paths') and inspect(c).has_table('learning_path_rooms')
    engine.dispose()

@pytest.mark.parametrize('actor,status',[('learner',403),('admin',201)])
def test_path_creator_role_cannot_be_self_assigned(env,actor,status):
    assert path(env,owner=actor).status_code==status
    assert env.client.post('/api/paths',json={'title':'Unauthorized','category':'linux'}).status_code in (401,403)
    assert env.client.post('/api/paths',headers=env.header('owner'),json={'title':'Bad escalation','category':'linux','creator_id':env.ids['other']}).status_code==422


def test_path_reference_never_grants_edit_permissions(env):
    with env.maker() as db:db.get(Room,2).status='published';db.commit()
    pid=path(env).json()['id'];assert membership(env,pid,[2]).status_code==200
    assert env.client.post('/api/rooms/2/tasks',headers=env.header('owner'),json={'title':'Unauthorized'}).status_code==404
    assert env.client.patch('/api/rooms/2',headers=env.header('owner'),json={'name':'Unauthorized'}).status_code==404


def test_room_type_filter_and_metadata_bounds(env):
    rid=newroom(env).json()['id'];publish(env,rid)
    assert env.client.get('/api/public/rooms?room_type=THEORY').json()['items'][0]['id']==rid
    assert env.client.get('/api/public/rooms?room_type=PRACTICAL').json()['total']==0
    assert env.client.get('/api/public/rooms?room_type=invalid').status_code==422
    assert env.client.patch(f'/api/rooms/{rid}',headers=env.header('owner'),json={'objectives':'x'*2001}).status_code==422


def test_migration_refuses_incompatible_existing_type_and_rolls_back():
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY)'));c.execute(text("CREATE TABLE rooms(id INTEGER PRIMARY KEY, room_type INTEGER NOT NULL DEFAULT 1)"))
    with pytest.raises(RuntimeError):
        with engine.begin() as c:migrate(c)
    with engine.connect() as c:assert c.scalar(text('SELECT count(*) FROM rooms'))==0
    engine.dispose()


def test_path_throttle_covers_read_and_write_routes_without_credentials():
    from app.login_limit import PathRateLimitMiddleware
    middleware=PathRateLimitMiddleware(None)
    for path in ['/api/paths','/api/paths/1/rooms','/api/paths/1/progress','/api/public/paths','/api/public/paths/1']:
        assert middleware.matches({'path':path,'method':'POST'})
    assert not middleware.matches({'path':'/api/rooms','method':'GET'})
