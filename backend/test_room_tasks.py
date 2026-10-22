"""Presentation grouping must preserve grading, ownership and progress."""
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,event,select,text,inspect,func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
from app.models import RoomTask,User,Room,ChallengeQuestion,QuestionSolve,RoomProgress
from app.database import Base,get_db
from app.main import app
from app.security import create_access_token
from app.config import settings
from app.room_tasks_migration import migrate
@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings,'reconciliation_enabled',False)
    engine=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False});event.listen(engine,'connect',lambda c,r:c.execute('PRAGMA foreign_keys=ON'));Base.metadata.create_all(engine);maker=sessionmaker(bind=engine)
    with maker() as db:
        actors=[User(username=n,email=n+'@example.com',password_hash='unused',role=role) for n,role in [('owner','creator'),('other','creator'),('learner','learner'),('admin','admin')]];db.add_all(actors);db.flush();ids={u.username:u.id for u in actors}
        db.add_all([Room(id=1,creator_id=ids['owner'],name='Owned draft',category='linux',difficulty='easy',status='draft'),Room(id=2,creator_id=ids['other'],name='Other room',category='web-security',difficulty='easy',status='draft')]);db.commit()
    old=dict(app.dependency_overrides)
    def dependency():
        with maker() as db:yield db
    app.dependency_overrides[get_db]=dependency
    with TestClient(app) as client:yield SimpleNamespace(client=client,maker=maker,engine=engine,ids=ids,header=lambda name:{'Authorization':'Bearer '+create_access_token(ids[name], 'learner' if name=='learner' else 'admin' if name=='admin' else 'creator')})
    app.dependency_overrides.clear();app.dependency_overrides.update(old);engine.dispose()
def task(env,title='Explore Linux',room=1,name='owner',instructions='## Learn\nRead files safely.'):
    return env.client.post(f'/api/rooms/{room}/tasks',headers=env.header(name),json={'title':title,'instructions':instructions})
def question(env,task_id=None):return env.client.post('/api/rooms/1/questions',headers=env.header('owner'),json={'question_text':'Discover it','answer':'synthetic-only-flag','points':50,'task_id':task_id})
def publish(env):
    with env.maker() as db:db.get(Room,1).status='published';db.commit()

def test_task_crud_reorder_and_remove_keeps_questions(env):
    t=task(env).json();second=task(env,'Permissions').json();q=question(env,t['id']).json()
    r=env.client.put('/api/rooms/1/tasks/order',headers=env.header('owner'),json={'task_ids':[second['id'],t['id']]});assert r.status_code==200 and [x['display_order'] for x in r.json()['tasks']]==[1,2]
    r=env.client.patch('/api/rooms/1/tasks/'+str(t['id']),headers=env.header('owner'),json={'title':'Edited','instructions':'**Actual material**'});assert r.status_code==200 and r.json()['instructions']=='**Actual material**'
    with env.maker() as db:before=(db.get(ChallengeQuestion,q['id']).answer_digest,db.get(ChallengeQuestion,q['id']).points)
    assert env.client.delete('/api/rooms/1/tasks/'+str(t['id']),headers=env.header('owner')).status_code==204
    with env.maker() as db:
        saved=db.get(ChallengeQuestion,q['id']);assert saved.task_id is None and (saved.answer_digest,saved.points)==before
        assert db.scalar(select(RoomTask.display_order))==1

@pytest.mark.parametrize('name,status',[('learner',403),('other',404)])
def test_only_owner_or_admin_can_write_tasks(env,name,status):assert task(env,name=name).status_code==status

def test_admin_and_guest_draft_published_boundaries(env):
    assert task(env,name='admin').status_code==404
    assert task(env,name='owner').status_code==201
    assert env.client.get('/api/rooms/1/tasks').status_code in (401,403)
    assert env.client.get('/api/rooms/1/tasks',headers=env.header('learner')).status_code==404
    assert env.client.get('/api/rooms/1/tasks',headers=env.header('other')).status_code==404
    publish(env);r=env.client.get('/api/rooms/1/tasks',headers=env.header('learner'));assert r.status_code==200
    assert set(r.json()['tasks'][0])=={'id','title','instructions','display_order'}
    public=env.client.get('/api/public/rooms/1').json();assert set(public)=={'id','name','description','category','difficulty','room_type','objectives','prerequisites','points_total','categories'} and 'instructions' not in str(public)

@pytest.mark.parametrize('fields',[{'title':' '},{'title':'x'*121},{'title':'bad\x00'},{'title':'Normal','instructions':'x'*12001},{'title':'Normal','instructions':'bad\x00'},{'title':'Normal','role':'admin'}])
def test_task_input_limits_and_no_role_changes(env,fields):assert env.client.post('/api/rooms/1/tasks',headers=env.header('owner'),json=fields).status_code==422

def test_cross_room_assignment_rejected_by_api_and_database(env):
    foreign=task(env,room=2,name='other').json()['id'];assert question(env,foreign).status_code==422
    local=task(env).json()['id'];q=question(env,local).json()['id']
    assert env.client.patch(f'/api/rooms/1/questions/{q}',headers=env.header('owner'),json={'task_id':foreign}).status_code==422
    with env.maker() as db:
        db.get(ChallengeQuestion,q).task_id=foreign
        with pytest.raises(IntegrityError):db.commit()
        db.rollback();assert db.get(ChallengeQuestion,q).task_id==local

@pytest.mark.parametrize('ids',[[999],[1,1],[]])
def test_invalid_order_is_transactional(env,ids):
    task(env);before=env.client.get('/api/rooms/1/tasks',headers=env.header('owner')).json()
    assert env.client.put('/api/rooms/1/tasks/order',headers=env.header('owner'),json={'task_ids':ids}).status_code==422
    assert env.client.get('/api/rooms/1/tasks',headers=env.header('owner')).json()==before

def test_grading_duplicate_credit_completion_and_presentation_edits(env):
    t=task(env).json()['id'];q=question(env,t).json()['id'];publish(env)
    def submit(answer):return env.client.post(f'/api/rooms/1/questions/{q}/submit',headers=env.header('learner'),json={'answer':answer}).json()
    assert not submit('incorrect')['correct'];r=submit('synthetic-only-flag');assert r['points_awarded']==50 and r['progress']['completed']
    assert submit('synthetic-only-flag')['points_awarded']==0
    assert env.client.patch(f'/api/rooms/1/questions/{q}',headers=env.header('owner'),json={'task_id':None}).status_code==409
    assert env.client.patch(f'/api/rooms/1/questions/{q}',headers=env.header('owner'),json={'points':100}).status_code==409
    url='/api/rooms/1/workspace'
    with env.maker() as db:db.get(Room,1).room_type='THEORY';db.commit()
    r=env.client.patch(url,headers=env.header('owner'),json={'revision':0,'action':'task_remove','data':{'id':t}})
    assert r.status_code==200
    assert env.client.get('/api/rooms/1/questions',headers=env.header('learner')).json()['questions'][0]['task_id']==t
    assert env.client.post(url+'/publish',headers=env.header('owner'),json={'revision':r.json()['revision']}).status_code==200
    stat=env.client.get('/api/users/me/learning',headers=env.header('learner')).json();assert stat['stats']['points']==50 and stat['stats']['rooms_completed']==1
    ranked=env.client.get('/api/public/leaderboard').json();assert ranked['items'][0]['points']==50
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(QuestionSolve))==1 and db.get(ChallengeQuestion,q).points==50
    rows=env.client.get('/api/rooms/1/questions',headers=env.header('learner')).json();assert rows['questions'][0]['task_id'] is None and 'synthetic-only-flag' not in str(rows)

def test_task_cap_and_no_question_side_effects(env):
    for i in range(30):assert task(env,str(i)).status_code==201
    assert task(env,'Too many').status_code==409
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(ChallengeQuestion))==0

def test_additive_legacy_migration_preserves_ids_answers_and_order(env):
    q=question(env).json()['id']
    with env.engine.begin() as c:
        before=dict(c.execute(text('SELECT id,answer_digest,answer_salt,points,display_order FROM challenge_questions')).mappings().one())
        # Existing nullable memberships emulate pre-task rows; reruns never backfill.
        assert migrate(c) and not migrate(c)
        after=dict(c.execute(text('SELECT id,answer_digest,answer_salt,points,display_order FROM challenge_questions')).mappings().one());assert after==before
        assert c.scalar(text('SELECT task_id FROM challenge_questions WHERE id=:id'),{'id':q}) is None

def test_real_old_sqlite_schema_gets_membership_without_table_rewrite():
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('PRAGMA foreign_keys=ON'));c.execute(text('CREATE TABLE rooms(id INTEGER PRIMARY KEY)'));c.execute(text('INSERT INTO rooms VALUES(1)'))
        c.execute(text('CREATE TABLE challenge_questions(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,answer_digest VARCHAR(64))'));c.execute(text("INSERT INTO challenge_questions VALUES(1,1,'unchanged')"))
        assert migrate(c) and not migrate(c)
        assert c.scalar(text('SELECT answer_digest FROM challenge_questions'))=='unchanged'
        assert c.scalar(text('SELECT task_id FROM challenge_questions')) is None
    engine.dispose()

def test_draft_deletion_cascades_only_its_task_and_question_records(env):
    t=task(env).json()['id'];question(env,t)
    assert env.client.delete('/api/rooms/1',headers=env.header('owner')).status_code==204
    with env.maker() as db:assert db.get(Room,1) is None and db.get(Room,2) is not None and not list(db.scalars(select(RoomTask)))
