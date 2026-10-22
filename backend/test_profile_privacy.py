"""Opt-in profile disclosure and revocation on owned isolated data."""
import pytest
from sqlalchemy import text, select
from app.models import User
from app.profile_privacy_migration import migrate
from test_leaderboard import env, award, board
from test_profile_v2 import picture
from app.config import settings

@pytest.fixture
def store(tmp_path, monkeypatch):
    path=tmp_path/'avatars';path.mkdir(mode=0o700)
    monkeypatch.setattr(settings,'avatar_storage_dir',str(path))
    return path

def upload(e,name='first'):
    return e.client.put('/api/users/me/avatar',headers={**e.header(name),'Content-Type':'image/png'},content=picture())
def edit(e,**data):return e.client.patch('/api/users/me',headers=e.header('first'),json=data)

def test_existing_photos_and_bios_stay_private_until_explicit_opt_in(env,store):
    award(env,'first',1);response=upload(env);assert response.status_code==200
    assert response.json()['avatar_public'] is False and response.json()['bio_public'] is False
    public=env.client.get('/api/public/learners/first').json()
    assert public['avatar_url'] is None and public['bio'] is None
    assert board(env).json()['items'][0]['avatar_url'] is None
    assert env.client.get('/api/public/learners/first/avatar').status_code==404
    assert env.client.get('/api/users/me/avatar',headers=env.header('first')).status_code==200

def test_opt_in_serves_only_reencoded_image_and_revocation_blocks_old_url(env,store):
    award(env,'first',1);key=upload(env).json()['avatar_version']
    assert edit(env,avatar_public=True,bio_public=True).status_code==200
    public=env.client.get('/api/public/learners/first').json()
    assert public['bio']=='Private bio' and key not in public['avatar_url']
    assert board(env).json()['items'][0]['avatar_url']==public['avatar_url']
    image=env.client.get(public['avatar_url']);assert image.status_code==200 and image.headers['content-type']=='image/png'
    assert image.content==(store/(key+'.png')).read_bytes() and image.headers['cache-control']=='no-store'
    assert not any(field in public for field in ('id','email','role','password_hash','avatar_version','google_identity','token_version','avatar_public','bio_public'))
    assert edit(env,avatar_public=False,bio_public=False).status_code==200
    assert env.client.get(public['avatar_url']).status_code==404
    assert env.client.get('/api/public/learners/first').json()['bio'] is None

def test_changes_and_removal_preserve_identity_legacy_preferences_and_credit(env,store):
    award(env,'first',1,75);upload(env);edit(env,avatar_public=True,bio_public=True)
    before=env.client.get('/api/public/learners/first').json()['avatar_url']
    upload(env);after=env.client.get('/api/public/learners/first').json()['avatar_url'];assert before!=after
    r=edit(env,username='renamed',display_name='New name');assert r.status_code==200 and r.json()['avatar_public'] is True
    assert env.client.get('/api/public/learners/first/avatar').status_code==404
    assert env.client.get('/api/public/learners/renamed').json()['points']==75
    assert env.client.delete('/api/users/me/avatar',headers=env.header('first')).status_code==200
    public=env.client.get('/api/public/learners/renamed').json();assert public['avatar_url'] is None and public['points']==75
    with env.maker() as db:
        u=db.get(User,env.ids['first']);assert u.role=='learner' and (u.avatar_style,u.avatar_color)==('initials','red')

@pytest.mark.parametrize('data',[{'avatar_public':'true'},{'bio_public':None},{'avatar_public':1},{'user_id':1,'avatar_public':True},{'role':'admin','bio_public':True}])
def test_strict_privacy_inputs_and_no_role_or_target_override(env,data):
    assert env.client.patch('/api/users/me',headers=env.header('second'),json=data).status_code==422
    with env.maker() as db:assert not db.get(User,env.ids['first']).avatar_public

def test_private_avatar_cannot_be_exposed_by_another_user_guest_or_suspension(env,store,monkeypatch):
    award(env,'first',1);upload(env)
    assert env.client.patch('/api/users/me',json={'avatar_public':True}).status_code in (401,403)
    assert env.client.patch('/api/users/me',headers=env.header('second'),json={'avatar_public':True}).status_code==200
    assert env.client.get('/api/public/learners/first/avatar').status_code==404
    edit(env,avatar_public=True)
    monkeypatch.setattr(settings,'production',True);monkeypatch.setattr(settings,'private_beta_enabled',True);monkeypatch.setattr(settings,'private_beta_user_ids',[env.ids['second']])
    assert env.client.get('/api/public/learners/first/avatar').status_code==404
    monkeypatch.setattr(settings,'production',False)
    with env.maker() as db:db.get(User,env.ids['first']).suspended=True;db.commit()
    assert env.client.get('/api/public/learners/first/avatar').status_code==404

def test_privacy_migration_reruns_without_publishing_existing_values(env):
    with env.engine.begin() as c:
        c.execute(text('ALTER TABLE users DROP COLUMN avatar_public'));c.execute(text('ALTER TABLE users DROP COLUMN bio_public'))
        assert migrate(c) and not migrate(c)
        assert c.execute(text("SELECT avatar_public,bio_public,bio,avatar_version FROM users WHERE username='first'")).one()==(False,False,'Private bio','a'*32)
        c.execute(text("UPDATE users SET avatar_public=TRUE WHERE username='first'"));assert not migrate(c)
        assert c.scalar(text("SELECT avatar_public FROM users WHERE username='first'"))
