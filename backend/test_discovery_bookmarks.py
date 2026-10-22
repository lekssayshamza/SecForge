"""Isolated discovery, taxonomy and private bookmark security; never cloud access."""
from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func,create_engine,text,event,inspect
from app.models import Room,LearningPath,LearningPathRoom,ChallengeQuestion,RoomBookmark,PathBookmark,RoomProgress,QuestionSolve,User
from app.categories import SLUGS,legacy_category
from app.discovery_migration import migrate
from app.security import create_access_token
from test_room_tasks import env

@pytest.fixture
def catalog(env):
    with env.maker() as db:
        db.add(User(id=5,username='secondlearner',email='second@example.com',role='learner',password_hash='unused'))
        db.add_all([Room(id=3,creator_id=env.ids['owner'],name='Alpha Linux',description='Learn command line safely',category='linux',difficulty='easy',status='published',room_type='THEORY'),Room(id=4,creator_id=env.ids['owner'],name='Beta Web',description='Inspect an HTTP service',category='web-security',difficulty='hard',status='published'),Room(id=5,creator_id=env.ids['owner'],name='Gamma Linux',category='linux',difficulty='medium',status='published')]);db.flush()
        db.add_all([ChallengeQuestion(id=10,room_id=3,question_text='Hidden question',answer_salt='x'*32,answer_digest='a'*64,points=75,display_order=1),ChallengeQuestion(id=11,room_id=5,question_text='Another',answer_salt='x'*32,answer_digest='b'*64,points=100,display_order=1)])
        db.add_all([LearningPath(id=1,creator_id=env.ids['owner'],title='Linux journey',description='Command line and practice',category='linux',difficulty='easy',status='published'),LearningPath(id=2,creator_id=env.ids['other'],title='Hidden draft path',category='other',difficulty='hard',status='draft')]);db.flush()
        db.add_all([LearningPathRoom(path_id=1,room_id=3,display_order=1),LearningPathRoom(path_id=1,room_id=4,display_order=2)]);db.commit()
    env.second={'Authorization':'Bearer '+create_access_token(5,'learner')};return env

def url(kind='rooms',id=3):return f'/api/bookmarks/{kind}/{id}'
def add(e,kind='rooms',id=3,headers=None):return e.client.put(url(kind,id),headers=headers or e.header('learner'))

def test_taxonomy_canonical_input_and_exact_migration_aliases(catalog):
    e=catalog
    assert len(SLUGS)==23 and len(set(SLUGS))==23
    for value in ['linux','web-security','ot-ics-security']:
        assert e.client.post('/api/rooms',headers=e.header('owner'),json=dict(name='Canonical',category=value,difficulty='easy',room_type='THEORY')).status_code==201
    for value in ['Linux','web','arbitrary','', 'linux; DROP TABLE rooms']:
        assert e.client.post('/api/rooms',headers=e.header('owner'),json=dict(name='Bad category',category=value,difficulty='easy')).status_code==422
        assert e.client.patch('/api/rooms/1',headers=e.header('owner'),json={'category':value}).status_code==422
        assert e.client.post('/api/paths',headers=e.header('owner'),json=dict(title='Invalid',category=value)).status_code==422
    assert legacy_category(' WEB ')=='web-security' and legacy_category('OT/ICS')=='ot-ics-security' and legacy_category('Ambiguous experiment')=='other'

@pytest.mark.parametrize('query,ids',[('q=  ALPHA  linux ',[3]),('q=command   line',[3]),('category=linux&sort=alphabetical',[3,5]),('difficulty=hard&room_type=PRACTICAL',[4]),('sort=difficulty',[3,5,4]),('sort=alphabetical&page_size=1&page=2',[4]),('q=%25',[]),("q=%27%20OR%201%3D1%20--",[])])
def test_combined_metadata_search_order_and_pagination(catalog,query,ids):
    r=catalog.client.get('/api/public/rooms?'+query);assert r.status_code==200,r.text
    assert [i['id'] for i in r.json()['items']]==ids
    assert all(not ({'bookmarked','progress','creator_id','ami_id','private_ip','answer_digest'}&set(i)) for i in r.json()['items'])
    assert 'Hidden question' not in r.text

@pytest.mark.parametrize('query',['sort=popular','category=Linux','category=unknown','page_size=49','progress=completed','q='+'a'*101])
def test_public_invalid_or_private_filters_rejected(catalog,query):assert catalog.client.get('/api/public/rooms?'+query).status_code==422

def test_progress_filters_match_solve_ledger_and_are_scoped_before_pagination(catalog):
    e=catalog
    with e.maker() as db:
        db.add_all([QuestionSolve(question_id=10,learner_id=e.ids['learner'],points_awarded=75),QuestionSolve(question_id=11,learner_id=e.ids['learner'],points_awarded=100),RoomProgress(room_id=3,learner_id=e.ids['learner'],points_earned=75,completed_at=datetime.utcnow()),RoomProgress(room_id=5,learner_id=e.ids['learner'],points_earned=100)]);db.commit()
    for progress,ids in [('completed',[3]),('in-progress',[5]),('not-started',[4])]:
        r=e.client.get('/api/discovery/rooms?progress='+progress,headers=e.header('learner'));assert [i['id'] for i in r.json()['items']]==ids and r.headers['cache-control']=='private, no-store'
    assert e.client.get('/api/discovery/rooms?progress=completed',headers=e.second).json()['total']==0
    assert e.client.get('/api/discovery/paths',headers=e.header('learner')).json()['items'][0]['percent']==50

@pytest.mark.parametrize('kind,id',[('rooms',3),('paths',1)])
def test_add_remove_idempotency_ownership_and_session_persistence(catalog,kind,id):
    e=catalog
    for _ in range(2):assert add(e,kind,id).json()=={'bookmarked':True}
    assert e.client.get(url(kind,id),headers=e.header('learner')).json()=={'bookmarked':True}
    assert e.client.get(url(kind,id),headers=e.second).json()=={'bookmarked':False}
    assert e.client.get('/api/saved/'+kind,headers=e.second).json()['total']==0
    saved=e.client.get('/api/saved/'+kind,headers=e.header('learner'));assert saved.json()['total']==1 and saved.json()['items'][0]['id']==id
    assert saved.headers['cache-control']=='private, no-store'
    for _ in range(2):assert e.client.delete(url(kind,id),headers=e.header('learner')).json()=={'bookmarked':False}
    assert e.client.get('/api/saved/'+kind,headers=e.header('learner')).json()['total']==0
    with e.maker() as db:assert db.scalar(select(func.count()).select_from(QuestionSolve))==0 and db.scalar(select(func.count()).select_from(RoomProgress))==0

@pytest.mark.parametrize('kind,id',[('rooms',1),('paths',2),('rooms',999)])
def test_drafts_and_missing_targets_indistinguishable(catalog,kind,id):assert add(catalog,kind,id).status_code==404

@pytest.mark.parametrize('method',['get','put','delete'])
def test_guests_are_denied_and_creators_can_manage_own_bookmarks(catalog,method):
    e=catalog
    assert getattr(e.client,method)(url()).status_code in (401,403)
    assert getattr(e.client,method)(url(),headers=e.header('owner')).status_code==200
    assert e.client.get('/api/saved/rooms',headers=e.header('owner')).status_code==200


def test_unpublished_hidden_then_republished_without_losing_bookmark_and_cascades(catalog):
    e=catalog;add(e);add(e,'paths',1)
    with e.maker() as db:db.get(Room,3).status='draft';db.get(LearningPath,1).status='draft';db.commit()
    for kind,id in [('rooms',3),('paths',1)]:
        assert e.client.get('/api/saved/'+kind,headers=e.header('learner')).json()['total']==0
        assert e.client.get(url(kind,id),headers=e.header('learner')).status_code==404
        assert add(e,kind,id).status_code==404
    with e.maker() as db:db.get(Room,3).status='published';db.commit()
    assert e.client.get('/api/saved/rooms',headers=e.header('learner')).json()['total']==1
    with e.maker() as db:db.delete(db.get(Room,3));db.delete(db.get(LearningPath,1));db.commit();assert db.scalar(select(func.count()).select_from(RoomBookmark))==db.scalar(select(func.count()).select_from(PathBookmark))==0


def test_user_deletion_cascades_only_their_associations(catalog):
    e=catalog;add(e);add(e,headers=e.second);add(e,'paths',1)
    with e.maker() as db:
        db.delete(db.get(User,e.ids['learner']));db.commit()
        assert db.scalar(select(RoomBookmark.user_id))==5 and db.scalar(select(func.count()).select_from(PathBookmark))==0
        assert db.get(Room,3) and db.get(LearningPath,1)


def test_saved_pagination_boundaries_and_path_search(catalog):
    e=catalog
    for id in [3,4,5]:add(e,id=id)
    r=e.client.get('/api/saved/rooms?page=2&page_size=1',headers=e.header('learner'));assert r.json()['total']==3 and len(r.json()['items'])==1
    assert e.client.get('/api/saved/rooms?page=0',headers=e.header('learner')).status_code==422
    assert e.client.get('/api/public/paths?q=COMMAND  line&sort=alphabetical').json()['total']==1
    assert e.client.get('/api/public/paths?category=linux&difficulty=hard').json()['total']==0


def test_no_n_plus_one_and_no_personal_fields_on_public_profiles(catalog):
    e=catalog;add(e);queries=[]
    def capture(*args):queries.append(args[2])
    event.listen(e.engine,'before_cursor_execute',capture)
    r=e.client.get('/api/discovery/rooms',headers=e.header('learner'));assert r.status_code==200 and len(queries)<=4
    assert r.json()['items'][0]['points_total']==100
    queries.clear();r=e.client.get('/api/discovery/paths',headers=e.header('learner'));assert r.status_code==200 and len(queries)<=4
    r=e.client.get('/api/public/leaderboard');assert 'bookmark' not in r.text and 'saved' not in r.text


def test_category_migration_audits_unknowns_reruns_without_touching_other_fields(catalog):
    e=catalog
    with e.engine.begin() as c:
        c.execute(text("UPDATE rooms SET category='Uncertain Experimental Topic' WHERE id=3"));c.execute(text("UPDATE learning_paths SET category='Web' WHERE id=1"))
        before=c.execute(text('SELECT name,creator_id,status,created_at,updated_at FROM rooms WHERE id=3')).one()
        assert migrate(c);assert not migrate(c)
        assert tuple(c.execute(text('SELECT category,category_original FROM rooms WHERE id=3')).one())==('other','Uncertain Experimental Topic')
        assert tuple(c.execute(text('SELECT category,category_original FROM learning_paths WHERE id=1')).one())==('web-security','Web')
        assert c.execute(text('SELECT name,creator_id,status,created_at,updated_at FROM rooms WHERE id=3')).one()==before
        c.execute(text("UPDATE rooms SET category='linux' WHERE id=3"));assert not migrate(c)
        assert c.scalar(text('SELECT category_original FROM rooms WHERE id=3'))=='Uncertain Experimental Topic'
