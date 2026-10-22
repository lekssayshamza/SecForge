"""Owned fixtures only: capability boundaries, relationship privacy and non-scoring previews."""
import asyncio
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select,func,text,inspect,delete
from app.models import User,UserFollow,UserBlock,QuestionSolve,RoomProgress,LabInstance,Room,RoomTemplate,MachineTemplate
from app.social_migration import migrate
from app.login_limit import SocialRateLimitMiddleware
from test_challenges import env,add,publish


def call(e,method,path,who='learner',**kw):
    return getattr(e.client,method)(path,headers=e.headers[who],**kw)

def test_follow_idempotency_counts_lists_privacy_and_unfollow(env):
    url='/api/social/users/creator/follow'
    for _ in range(2):
        r=call(env,'put',url);assert r.status_code==200 and r.json()['following_user']
    assert env.client.get('/api/public/learners/creator/connections').json()=={'followers':1,'following':0}
    values=env.client.get('/api/public/learners/creator/connections/followers').json()
    assert values['total']==1 and values['items'][0]['username']=='learner'
    assert set(values['items'][0])=={'username','display_name','avatar_style','avatar_color','avatar_url'}
    assert env.client.get('/api/public/learners/learner/connections/following').json()['items'][0]['username']=='creator'
    for _ in range(2):assert call(env,'delete',url).status_code==200
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(UserFollow))==0
    assert env.client.get('/api/public/learners/creator').status_code==200 # unranked supported

@pytest.mark.parametrize('who',['learner','creator','admin'])
def test_all_supported_accounts_can_follow_and_use_learning_saved_paths(env,who):
    target='other' if who!='other' else 'learner'
    assert call(env,'put',f'/api/social/users/{target}/follow',who).status_code==200
    for path in ['/api/users/me/learning','/api/discovery/rooms','/api/discovery/paths','/api/saved/rooms','/api/saved/paths','/api/leaderboard/me']:
        assert call(env,'get',path,who).status_code==200,path
    assert call(env,'put',f'/api/bookmarks/rooms/{env.other_room}',who).status_code==200

@pytest.mark.parametrize('kind',['follow','block'])
def test_self_guest_invalid_and_suspended_target(env,kind):
    url='/api/social/users/learner/'+kind
    assert call(env,'put',url).status_code==422
    assert env.client.put('/api/social/users/creator/'+kind).status_code in (401,403)
    assert call(env,'put','/api/social/users/does-not-exist/'+kind).status_code==404
    with env.maker() as db:db.get(User,env.users['creator']).suspended=True;db.commit()
    assert call(env,'put','/api/social/users/creator/'+kind).status_code==404


def test_block_deletes_both_directions_prevents_recreation_and_unblock_does_not_restore(env):
    assert call(env,'put','/api/social/users/creator/follow').status_code==200
    assert call(env,'put','/api/social/users/learner/follow','creator').status_code==200
    for _ in range(2):assert call(env,'put','/api/social/users/creator/block').status_code==200
    for who,target in [('learner','creator'),('creator','learner')]:
        assert call(env,'put',f'/api/social/users/{target}/follow',who).status_code==403
    with env.maker() as db:
        assert db.scalar(select(func.count()).select_from(UserFollow))==0
        assert db.scalar(select(func.count()).select_from(UserBlock))==1
    assert call(env,'delete','/api/social/users/learner/block','creator').status_code==200 # cannot remove other user's block
    assert call(env,'put','/api/social/users/learner/follow','creator').status_code==403
    assert call(env,'delete','/api/social/users/creator/block').status_code==200
    assert env.client.get('/api/public/learners/creator/connections').json()['followers']==0
    assert call(env,'put','/api/social/users/learner/follow','creator').status_code==200


def test_suspended_actor_and_hidden_counts_and_foreign_key_cleanup(env):
    assert call(env,'put','/api/social/users/creator/follow').status_code==200
    with env.maker() as db:db.get(User,env.users['learner']).suspended=True;db.commit()
    assert call(env,'put','/api/social/users/other/follow').status_code==403
    assert env.client.get('/api/public/learners/creator/connections').json()['followers']==0
    with env.maker() as db:
        db.execute(delete(User).where(User.id==env.users['learner']));db.commit()
        assert db.scalar(select(func.count()).select_from(UserFollow))==0

@pytest.mark.parametrize('path',['followers','following'])
def test_connection_pagination_and_input_bounds(env,path):
    for who in ['learner','admin','other']:
        assert call(env,'put','/api/social/users/creator/follow',who).status_code==200
    first=env.client.get('/api/public/learners/creator/connections/followers?page_size=1').json()
    second=env.client.get('/api/public/learners/creator/connections/followers?page_size=1&page=2').json()
    assert first['total']==3 and first['items']!=second['items']
    for query in ['page=0','page_size=51','page=1001']:
        assert env.client.get(f'/api/public/learners/creator/connections/{path}?{query}').status_code==422
    assert env.client.get('/api/public/learners/creator/connections/secrets').status_code==422

@pytest.mark.parametrize('who',['creator','admin','learner'])
def test_scoring_other_owners_questions_and_ranking_for_all_roles(env,who):
    q=add(env).json()['id'];publish(env)
    if who=='creator':
        # Move authored content to the other creator to exercise legitimate learning.
        with env.maker() as db:db.get(Room,env.room).creator_id=env.users['other'];db.commit()
    url=f'/api/rooms/{env.room}/questions/{q}/submit'
    bad=call(env,'post',url,who,json={'answer':'incorrect'});assert bad.status_code==200 and not bad.json()['correct']
    correct=call(env,'post',url,who,json={'answer':'SF{offline-only}'});assert correct.status_code==200 and correct.json()['points_awarded']==100
    assert call(env,'post',url,who,json={'answer':'SF{offline-only}'}).json()['points_awarded']==0
    assert call(env,'get',f'/api/rooms/{env.room}/progress',who).json()['completed']
    assert call(env,'get','/api/leaderboard/me',who).json()['rank']==1
    assert call(env,'get','/api/users/me/learning',who).json()['stats']['points']==100

@pytest.mark.parametrize('who',['creator','admin'])
def test_own_room_preview_cannot_award_or_start_learner_machine(env,who):
    q=add(env).json()['id'];publish(env)
    if who=='admin':
        with env.maker() as db:db.get(Room,env.room).creator_id=env.users['admin'];db.commit()
    assert call(env,'get',f'/api/rooms/{env.room}/questions',who).status_code==200
    assert call(env,'get',f'/api/rooms/{env.room}/progress',who).json()['points_earned']==0
    assert call(env,'post',f'/api/rooms/{env.room}/questions/{q}/submit',who,json={'answer':'SF{offline-only}'}).status_code==403
    assert call(env,'post',f'/api/rooms/{env.room}/lab',who,json={'ttl_hours':1}).status_code==403
    with env.maker() as db:
        for model in [QuestionSolve,RoomProgress,LabInstance]:assert db.scalar(select(func.count()).select_from(model))==0


def test_creator_admin_authorization_is_separate_from_learning(env):
    assert call(env,'get','/api/admin','creator').status_code==403
    assert call(env,'get','/api/creator/analytics','learner').status_code==403
    for who in ['creator','admin']:
        assert call(env,'post','/api/rooms',who,json={'name':'Owned fixture','description':'Test','category':'linux','difficulty':'easy','room_type':'THEORY'}).status_code==201
    assert call(env,'post','/api/rooms','learner',json={'name':'Denied','category':'linux','difficulty':'easy'}).status_code==403


def test_social_migration_reruns_preserve_users_and_ledger(env):
    with env.maker() as db:
        before=list(db.execute(select(User.id,User.username,User.role)))
        db.execute(text('DROP TABLE user_follows'));db.execute(text('DROP TABLE user_blocks'));db.commit()
    with env.maker().bind.begin() as c:
        assert migrate(c);assert not migrate(c)
        assert set(inspect(c).get_table_names())>={'user_follows','user_blocks'}
    with env.maker() as db:assert list(db.execute(select(User.id,User.username,User.role)))==before


def test_social_write_rate_limit_is_bounded_and_separate_from_reads():
    async def downstream(scope,receive,send):
        await send({'type':'http.response.start','status':204,'headers':[]});await send({'type':'http.response.body','body':b''})
    async def scenario():
        middleware=SocialRateLimitMiddleware(downstream,enabled=True,limit=2)
        async def request(method,path):
            events=[]
            async def send(e):events.append(e)
            await middleware({'type':'http','method':method,'path':path,'client':('192.0.2.1',1)},None,send)
            return events[0]['status']
        assert await request('PUT','/api/social/users/creator/follow')==204
        assert await request('DELETE','/api/social/users/creator/follow')==204
        assert await request('PUT','/api/social/users/creator/follow')==429
        assert await request('GET','/api/public/learners/creator/connections')==204
    asyncio.run(scenario())

@pytest.mark.parametrize('who',['creator','admin'])
def test_creator_learning_launch_keeps_learner_timer_and_exact_image(env,who,monkeypatch):
    from app import labs
    calls=[];monkeypatch.setattr(labs,'provision_lab_background',lambda *args:calls.append(args))
    with env.maker() as db:
        template=RoomTemplate(room_id=env.other_room,created_by=env.users['other'],version=1,is_published=True);db.add(template);db.flush()
        db.add(MachineTemplate(room_template_id=template.id,ami_id='ami-only-isolated',status='available'));db.commit()
    response=call(env,'post',f'/api/rooms/{env.other_room}/lab',who,json={'ttl_hours':4})
    assert response.status_code==201,response.json()
    value=response.json();assert value['session_started_at'] and value['extensions_used']==0
    start=datetime.fromisoformat(value['session_started_at']);end=datetime.fromisoformat(value['expires_at']);assert (end-start).total_seconds()==3600
    assert calls and calls[0][-1]=='ami-only-isolated'
    assert call(env,'get',f'/api/rooms/{env.other_room}/lab',who).json()['id']==value['id']
    assert call(env,'get',f'/api/rooms/{env.other_room}/lab','learner').status_code==404


def test_hint_draft_publish_preserves_saved_answer_question_id_and_award(env):
    q=add(env,hint='Published hint').json()['id'];publish(env)
    answer={'answer':'SF{offline-only}'}
    assert call(env,'post',f'/api/rooms/{env.room}/questions/{q}/submit',json=answer).json()['points_awarded']==100
    path=f'/api/rooms/{env.room}/workspace'
    draft=call(env,'get',path,'creator').json()
    edit=call(env,'patch',path,'creator',json={'revision':draft['revision'],'action':'question_update','data':{'id':q,'hint':'Draft hint'}})
    assert edit.status_code==200
    assert call(env,'get',f'/api/rooms/{env.room}/questions').json()['questions'][0]['hint']=='Published hint'
    assert call(env,'post',path+'/publish','creator',json={'revision':edit.json()['revision']}).status_code==200
    question=call(env,'get',f'/api/rooms/{env.room}/questions').json()['questions'][0]
    assert question['id']==q and question['hint']=='Draft hint' and 'answer' not in question
    assert call(env,'post',f'/api/rooms/{env.room}/questions/{q}/submit',json=answer).json()['points_awarded']==0
    edit=call(env,'patch',path,'creator',json={'revision':call(env,'get',path,'creator').json()['revision'],'action':'question_update','data':{'id':q,'hint':None}})
    assert edit.status_code==200
    assert call(env,'post',path+'/publish','creator',json={'revision':edit.json()['revision']}).status_code==200
    assert call(env,'get',f'/api/rooms/{env.room}/questions').json()['questions'][0]['hint'] is None
