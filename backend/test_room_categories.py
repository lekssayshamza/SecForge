"""Multi-category metadata uses the existing draft/publication and discovery rules."""
import pytest
from sqlalchemy import select,func,text
from test_challenges import env,add,submit,progress
from test_room_workspace import theory,edit,release,read
from app.models import Room,RoomCategory,RoomDraft,QuestionSolve
from app.room_categories_migration import migrate


def test_creation_multi_categories_and_legacy_primary_compatibility(env):
    r=env.client.post('/api/rooms',headers=env.headers['creator'],json={'name':'Synthetic multi','category':'linux','categories':['linux','web-security'],'difficulty':'easy','room_type':'THEORY'})
    assert r.status_code==201,r.text
    assert r.json()['category']=='linux' and r.json()['categories']==['linux','web-security']
    legacy=env.client.post('/api/rooms',headers=env.headers['creator'],json={'name':'Legacy caller','category':'networking','difficulty':'easy'})
    assert legacy.status_code==201 and legacy.json()['categories']==['networking']
    assert env.client.post('/api/rooms',headers=env.headers['learner'],json={'name':'Forbidden','category':'linux','difficulty':'easy'}).status_code==403

@pytest.mark.parametrize('values',[[],['linux','linux'],['Web'],['arbitrary'],['linux']*9,[1],['linux',None]])
def test_invalid_categories_rejected_on_creation_and_drafts(env,values):
    assert env.client.post('/api/rooms',headers=env.headers['creator'],json={'name':'Invalid','category':'linux','categories':values,'difficulty':'easy'}).status_code==422
    assert edit(env,'metadata',{'categories':values}).status_code==422

def test_category_primary_conflict_rejected(env):
    assert edit(env,'metadata',{'category':'linux','categories':['web-security','linux']}).status_code==422
    assert env.client.post('/api/rooms',headers=env.headers['creator'],json={'name':'Invalid','category':'linux','categories':['web-security'],'difficulty':'easy'}).status_code==422


def test_draft_isolation_publication_filters_search_and_no_duplicate_rows(env):
    theory(env);q=add(env).json()['id'];assert release(env).status_code==200
    assert submit(env,q).json()['points_awarded']==100
    before=progress(env)
    assert edit(env,'metadata',{'categories':['linux','web-security']}).status_code==200
    assert read(env)['room']['categories']==['linux','web-security']
    assert env.client.get(f'/api/public/rooms/{env.room}').json()['categories']==['web-security']
    assert release(env).status_code==200
    public=env.client.get(f'/api/public/rooms/{env.room}').json()
    assert public['category']=='linux' and public['categories']==['linux','web-security']
    for category in ['linux','web-security','linux,web-security','web-security,networking']:
        page=env.client.get('/api/public/rooms',params={'category':category}).json()
        assert [r['id'] for r in page['items']]==[env.room] and page['total']==1
        for role in ['learner','admin']:
            page=env.client.get('/api/discovery/rooms',params={'category':category},headers=env.headers[role]).json()
            assert [r['id'] for r in page['items']]==[env.room]
    for word in ['LINUX','Web Security']:
        assert env.client.get('/api/public/rooms',params={'q':word}).json()['total']==1
    assert progress(env)==before
    assert submit(env,q).json()['points_awarded']==0
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(QuestionSolve))==1
    # Explicit old single-category writes reset the associations coherently.
    assert edit(env,'metadata',{'category':'networking'}).status_code==200
    assert release(env).status_code==200
    assert env.client.get(f'/api/public/rooms/{env.room}').json()['categories']==['networking']
    assert env.client.get('/api/public/rooms',params={'category':'linux'}).json()['total']==0


def test_ownership_draft_visibility_filter_bounds_and_pagination(env):
    theory(env)
    assert edit(env,'metadata',{'categories':['linux','web-security']}).status_code==200
    assert env.client.get('/api/public/rooms',params={'category':'linux'}).json()['total']==0
    assert env.client.patch(f'/api/rooms/{env.room}/workspace',headers=env.headers['other'],json={'revision':read(env)['revision'],'action':'metadata','data':{'categories':['networking']}}).status_code==404
    for category in ['linux,invalid','linux,' ,','.join(['linux']*9)]:assert env.client.get('/api/public/rooms',params={'category':category}).status_code==422
    assert release(env).status_code==200
    page=env.client.get('/api/public/rooms',params={'category':'linux,web-security','page_size':1}).json()
    assert page['total']==1 and len(page['items'])==1
    assert env.client.get('/api/public/rooms',params={'category':'linux,web-security','page':2,'page_size':1}).json()['items']==[]


def test_additive_migration_backfills_once_and_preserves_rooms_and_drafts(env):
    with env.maker() as db:
        for room in db.scalars(select(Room)):room.category='linux'
        db.commit();engine=db.get_bind()
    with engine.begin() as c:
        before=c.execute(text('SELECT * FROM rooms ORDER BY id')).all()
        RoomCategory.__table__.drop(c)
        assert migrate(c) and not migrate(c)
        assert c.execute(text('SELECT * FROM rooms ORDER BY id')).all()==before
        assert c.scalar(text('SELECT count(*) FROM room_categories'))==2


def test_unknown_existing_category_stops_migration_without_rewriting(env):
    with env.maker() as db:engine=db.get_bind()
    with engine.begin() as c:
        before=c.execute(text('SELECT id,category FROM rooms ORDER BY id')).all()
        with pytest.raises(RuntimeError,match='Review noncanonical'):migrate(c)
        assert c.execute(text('SELECT id,category FROM rooms ORDER BY id')).all()==before
