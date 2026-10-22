"""Guided creator discovery: real metadata, no private foreign drafts or answers."""
import pytest
from sqlalchemy import select,event
from app.models import Room,RoomCategory,ChallengeQuestion,RoomDraft
from test_room_tasks import env
from test_education_paths import newroom,path,publish,membership,publishpath

def test_creator_search_filters_secondary_category_and_safe_points(env):
    with env.maker() as db:
        r=db.get(Room,1);r.description='Learn investigation safely';r.category_links=[RoomCategory(category='linux'),RoomCategory(category='web-security')]
        db.commit()
    headers=env.header('owner')
    response=env.client.get('/api/paths/eligible-rooms?q=  LEARN investigation &category=web-security&difficulty=easy',headers=headers)
    assert response.status_code==200,response.text
    item=response.json()['items'][0];assert item['id']==1 and item['publication_status']=='draft' and item['points_total']==0
    assert item['categories']==['linux','web-security']
    assert not any(k in item for k in ('creator_id','answer_digest','answer_salt','ami_id','private_ip','instructions','document'))
    assert env.client.get('/api/paths/eligible-rooms?category=web-security&difficulty=hard',headers=headers).json()['items']==[]
    assert env.client.get('/api/paths/eligible-rooms?q=%25',headers=headers).json()['items']==[]

def test_saved_draft_question_points_owner_only_and_public_release_points(env):
    rid=newroom(env).json()['id']
    q=env.client.post(f'/api/rooms/{rid}/questions',headers=env.header('owner'),json={'question_text':'Safe fixture','answer':'isolated-private-answer','points':20})
    assert q.status_code==201
    own=env.client.get('/api/paths/eligible-rooms',headers=env.header('owner'))
    item=next(r for r in own.json()['items'] if r['id']==rid)
    assert item['points_total']==20 and item['publication_status']=='draft'
    assert str(rid) not in [str(r['id']) for r in env.client.get('/api/paths/eligible-rooms',headers=env.header('other')).json()['items']]
    assert 'isolated-private-answer' not in own.text and 'answer_digest' not in own.text
    assert publish(env,rid).status_code==200
    pid=path(env).json()['id'];assert membership(env,pid,[rid]).status_code==200;assert publishpath(env,pid).status_code==200
    public=env.client.get(f'/api/public/paths/{pid}').json()['rooms'][0]
    assert public['points_total']==20 and 'publication_status' not in public
    owner=env.client.get(f'/api/paths/{pid}',headers=env.header('owner')).json()['rooms'][0]
    assert owner['points_total']==20 and owner['publication_status']=='published'

@pytest.mark.parametrize('query',['category=arbitrary','difficulty=invalid','page=0','page=1001','q='+'x'*101])
def test_creator_options_invalid_filters_are_rejected(env,query):
    assert env.client.get('/api/paths/eligible-rooms?'+query,headers=env.header('owner')).status_code==422

@pytest.mark.parametrize('actor,status',[('learner',403),('other',200),('admin',200)])
def test_discovery_permissions_no_private_foreign_content(env,actor,status):
    response=env.client.get('/api/paths/eligible-rooms',headers=env.header(actor));assert response.status_code==status
    if status==200:assert all(r['id']!=1 for r in response.json()['items'])
    assert env.client.get('/api/paths/eligible-rooms').status_code in (401,403)

def test_options_bounded_deterministic_and_constant_queries(env):
    with env.maker() as db:
        db.add_all([Room(creator_id=env.ids['owner'],name=f'Fixture {i:03}',category='linux',difficulty='easy',status='draft') for i in range(35)]);db.commit()
    calls=[]
    event.listen(env.engine,'before_cursor_execute',lambda *args:calls.append(args[2]))
    first=env.client.get('/api/paths/eligible-rooms',headers=env.header('owner')).json();assert len(first['items'])==24 and first['has_more']
    assert len(calls)<=5
    second=env.client.get('/api/paths/eligible-rooms?page=2',headers=env.header('owner')).json();assert not second['has_more']
    assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
    assert [r['id'] for r in first['items']]==sorted((r['id'] for r in first['items']),reverse=True)
