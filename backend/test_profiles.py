"""Offline profile, preservation, VPN filename and additive migration regressions."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import vpn_routes
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import User, VpnIdentity, Room, RoomProgress, QuestionSolve
from app.profile_identity import vpn_profile_filename
from app.profile_migration import migrate
from app.security import create_access_token, hash_password

PASSWORD = 'synthetic-test-password'
PASSWORD_HASH = hash_password(PASSWORD)

@pytest.fixture
def env():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False)
    with maker() as db:
        actors = [User(username=r+'_profile', email=r+'@example.com', password_hash=PASSWORD_HASH, role=r) for r in ['learner','creator','admin']]
        db.add_all(actors); db.commit(); ids = {u.role: u.id for u in actors}
    def dependency():
        with maker() as db: yield db
    old = dict(app.dependency_overrides); app.dependency_overrides[get_db] = dependency
    client = TestClient(app)
    yield SimpleNamespace(client=client, maker=maker, ids=ids, headers={r: {'Authorization':'Bearer '+create_access_token(uid,r)} for r,uid in ids.items()})
    client.close(); app.dependency_overrides.clear(); app.dependency_overrides.update(old); engine.dispose()


def edit(env, fields, role='learner'):
    return env.client.patch('/api/users/me', headers=env.headers[role], json=fields)

@pytest.mark.parametrize('role', ['learner','creator','admin'])
def test_profile_fields_sessions_and_roles(env, role):
    response = edit(env, dict(username='  NEW_'+role.upper()+'  ', display_name='  New name  ', avatar_style='terminal', avatar_color='blue'), role)
    assert response.status_code == 200
    value = response.json()
    assert value['bio'] is None and value['avatar_version'] is None and value['created_at']
    assert {k:v for k,v in value.items() if k not in ('bio','avatar_version','created_at','avatar_public','bio_public')} == dict(creator_access=False, id=env.ids[role], username='new_'+role, email=role+'@example.com', role=role, display_name='New name', avatar_style='terminal', avatar_color='blue', must_change_password=False, password_enabled=True)
    assert env.client.get('/api/auth/me', headers=env.headers[role]).json() == value
    assert env.client.post('/api/auth/login', json=dict(username='NEW_'+role.upper(), password=PASSWORD)).status_code == 200
    assert env.client.post('/api/auth/login', json=dict(username=role+'_profile', password=PASSWORD)).status_code == 401
    with env.maker() as db: assert db.get(User, env.ids[role]).password_hash == PASSWORD_HASH

@pytest.mark.parametrize('fields', [dict(role='admin'),dict(id=999),dict(user_id=999),dict(email='other@example.com'),dict(password='x'),dict(password_hash='x')])
def test_no_privilege_or_other_account_updates(env, fields):
    assert edit(env, fields).status_code == 422
    assert env.client.patch('/api/users/me', json=fields).status_code in (401,403)
    assert env.client.patch(f"/api/users/{env.ids['creator']}", headers=env.headers['learner'], json=dict(display_name='Other')).status_code in (404,405)
    with env.maker() as db:
        assert db.get(User, env.ids['learner']).role == 'learner'
        assert db.get(User, env.ids['creator']).display_name is None

@pytest.mark.parametrize('username', ['', 'ab', 'a'*51, 'two words', '../path', 'new/line', 'foo\nbar', 'éclair', None, 12])
def test_invalid_username(env, username):
    assert edit(env, dict(username=username)).status_code == 422


def test_collision_registration_and_database_enforcement(env):
    assert edit(env, dict(username=' CREATOR_PROFILE ')).status_code == 409
    with env.maker() as db:
        db.add(User(username='CREATOR_PROFILE', email='collision@example.com', password_hash=PASSWORD_HASH, role='learner'))
        with pytest.raises(IntegrityError): db.commit()
        db.rollback()
    assert env.client.post('/api/users', json=dict(username='CREATOR_PROFILE', email='another@example.com', password=PASSWORD)).status_code == 409
    response = env.client.post('/api/users', json=dict(username=' New.Registration-1 ', email='new@example.com', password=PASSWORD))
    assert response.status_code == 201 and response.json()['username'] == 'new.registration-1'
    assert response.json()['avatar_style'] == 'initials'


def test_legacy_login_and_other_fields_do_not_require_a_rename(env):
    with env.maker() as db:
        db.get(User, env.ids['learner']).username = 'LegacyMixed'; db.get(User, env.ids['learner']).email = 'legacy@example.invalid'; db.commit()
    assert env.client.post('/api/auth/login', json=dict(username='legacymixed', password=PASSWORD)).status_code == 200
    assert edit(env, dict(display_name='Legacy retained')).json()['username'] == 'LegacyMixed'

@pytest.mark.parametrize('fields', [dict(display_name='a'*81),dict(display_name='bad\x00name'),dict(avatar_style='https://evil.invalid/img'),dict(avatar_style='<svg>'),dict(avatar_style=None),dict(avatar_color='url(evil)'),dict(avatar_color=None)])
def test_avatar_and_display_name_validation(env, fields):
    assert edit(env, fields).status_code == 422


def test_clear_display_name_and_avatar_persistence(env):
    assert edit(env, dict(display_name='Label',avatar_style='code',avatar_color='purple')).status_code == 200
    assert edit(env, dict(display_name='   ')).json()['display_name'] is None
    with env.maker() as db:
        u=db.get(User,env.ids['learner']); assert (u.avatar_style,u.avatar_color)==('code','purple')
    assert env.client.get('/api/auth/me',headers=env.headers['learner']).json()['avatar_style']=='code'


def test_progress_solves_and_creator_ownership_survive_renames(env):
    h=env.headers
    r=env.client.post('/api/rooms',headers=h['creator'],json=dict(name='Preserved',category='linux',difficulty='easy',room_type='THEORY')).json()['id']
    q=env.client.post(f'/api/rooms/{r}/questions',headers=h['creator'],json=dict(question_text='Find it',answer='synthetic-only-flag',points=75)).json()['id']
    assert env.client.post(f'/api/rooms/{r}/publish',headers=h['creator']).status_code==200
    submit=lambda:env.client.post(f'/api/rooms/{r}/questions/{q}/submit',headers=h['learner'],json=dict(answer='synthetic-only-flag'))
    assert submit().status_code==200
    before=env.client.get(f'/api/rooms/{r}/progress',headers=h['learner']).json()
    assert edit(env,dict(username='renamed_learner')).status_code==200
    assert edit(env,dict(username='renamed_creator'),'creator').status_code==200
    assert env.client.get(f'/api/rooms/{r}/progress',headers=h['learner']).json()==before
    repeated=submit().json(); assert repeated['points_awarded']==0 and repeated['progress']['points_earned']==75
    assert env.client.get(f'/api/rooms/{r}/questions',headers=h['creator']).status_code==200
    with env.maker() as db:
        assert db.get(Room,r).creator_id==env.ids['creator']
        assert db.scalar(select(QuestionSolve.learner_id))==env.ids['learner']
        assert db.scalar(select(RoomProgress.points_earned))==75


def test_database_errors_are_sanitized(env,monkeypatch):
    def fail(*a,**kw):raise OperationalError('private sql',{},Exception('private diagnostic'))
    monkeypatch.setattr('sqlalchemy.orm.Session.commit',fail)
    r=edit(env,dict(display_name='Attempt'));assert r.status_code==503 and 'private' not in r.text


def test_filename_changes_but_vpn_identity_profile_and_mult_room_reuse_do_not(env,monkeypatch):
    monkeypatch.setattr(settings,'vpn_enabled',True)
    monkeypatch.setattr(vpn_routes,'sync_vpn_authz',lambda db:None)
    manager=MagicMock();monkeypatch.setattr(vpn_routes,'VpnManager',lambda:manager)
    calls=[]
    def profile(_manager,cn,ip):calls.append((cn,ip));return 'synthetic-profile-for-'+cn
    monkeypatch.setattr(vpn_routes,'build_client_profile',profile)
    uid=env.ids['learner']
    with env.maker() as db:db.add(VpnIdentity(user_id=uid,common_name=f'sf-{uid}',vpn_ip='10.8.0.65'));db.commit()
    first=env.client.get('/api/vpn/profile',headers={**env.headers['learner'],'Origin':'http://localhost:3000'},params=dict(room_id=1089))
    assert first.status_code==200 and first.headers['content-disposition']=='attachment; filename="secforge-learner_profile.ovpn"'
    assert 'content-disposition' in first.headers['access-control-expose-headers'].lower()
    assert edit(env,dict(username='new_learner')).status_code==200
    second=env.client.get('/api/vpn/profile',headers=env.headers['learner'],params=dict(room_id=3171))
    assert second.status_code==200 and second.content==first.content
    assert second.headers['content-disposition']=='attachment; filename="secforge-new_learner.ovpn"'
    assert calls==[(f'sf-{uid}','10.8.0.65')]*2
    manager.issue_client_certificate.assert_not_called()
    with env.maker() as db:
        identity=db.scalar(select(VpnIdentity).where(VpnIdentity.user_id==uid))
        assert identity.common_name==f'sf-{uid}' and identity.revoked_at is None

@pytest.mark.parametrize('name,expected',[('TestLearner2004','secforge-testlearner2004.ovpn'),('../danger\r\n"x','secforge-danger---x.ovpn'),('é','secforge-user.ovpn'),('a'*80,'secforge-'+('a'*50)+'.ovpn')])
def test_filename_safety(name,expected):assert vpn_profile_filename(name)==expected


def legacy(db,names=('LegacyUser',)):
    db.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(50) NOT NULL UNIQUE, email VARCHAR(255) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL)'))
    for i,n in enumerate(names,1):db.execute(text("INSERT INTO users VALUES (:id,:name,:email,'unchanged-hash','creator')"),dict(id=i,name=n,email=f'{i}@example.com'))


def test_additive_migration_preserves_legacy_accounts_and_is_idempotent():
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        legacy(db);assert migrate(db) and not migrate(db)
        assert db.execute(text('SELECT id,username,email,password_hash,role FROM users')).one()==(1,'LegacyUser','1@example.com','unchanged-hash','creator')
        assert db.execute(text('SELECT display_name,avatar_style,avatar_color FROM users')).one()==(None,'initials','red')
        with pytest.raises(IntegrityError):db.execute(text("INSERT INTO users(id,username,email,password_hash,role) VALUES (2,'legacyuser','second@example.com','unused','learner')"))
    engine.dispose()


def test_migration_refuses_collisions_before_adding_columns():
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        legacy(db,('LegacyUser','legacyuser'))
        with pytest.raises(RuntimeError):migrate(db)
        assert 'display_name' not in {c['name'] for c in inspect(db).get_columns('users')}
        assert db.scalar(text('SELECT count(*) FROM users'))==2
    engine.dispose()

@pytest.mark.parametrize('index',["CREATE UNIQUE INDEX uq_users_username_ci ON users(username)","CREATE UNIQUE INDEX uq_users_username_ci ON users(lower(username),email)","CREATE UNIQUE INDEX uq_users_username_ci ON users(lower(username)) WHERE role='creator'"])
def test_migration_refuses_unsafe_index(index):
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        legacy(db);db.execute(text(index))
        with pytest.raises(RuntimeError):migrate(db)
        assert 'display_name' not in {c['name'] for c in inspect(db).get_columns('users')}
    engine.dispose()
