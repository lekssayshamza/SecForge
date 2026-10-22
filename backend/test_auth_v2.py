"""Offline auth/OIDC tests. Provider responses are fake; RSA validation is real."""
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs,urlparse
from unittest.mock import MagicMock
import logging
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine,select,text,inspect,event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import google_auth as g
from app.auth_migration import migrate
from app.config import settings
from app.database import Base,get_db
from app.lab_time import utc_now
from app.main import app
from app.models import User,GoogleIdentity,OAuthAttempt
from app.security import hash_password,decode_access_token
P='synthetic-temporary-password';NEW='synthetic-new-long-passphrase';HASH=hash_password(P)
@pytest.fixture
def env(monkeypatch):
    for k,v in dict(production=False,private_beta_enabled=False,registration_enabled=True,google_oauth_enabled=True,google_client_id='synthetic-client',google_client_secret=SecretStr('synthetic-secret'),app_public_url='http://localhost:3000',google_redirect_uri='http://localhost:3000/api/auth/google/callback').items():monkeypatch.setattr(settings,k,v)
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    event.listen(engine,'connect',lambda c,r:c.execute('PRAGMA foreign_keys=ON'))
    Base.metadata.create_all(engine);maker=sessionmaker(bind=engine)
    with maker() as db:
        u=User(username='creator',email='owner@example.com',password_hash=HASH,role='creator',must_change_password=True);db.add(u);db.commit();uid=u.id
    def dependency():
        with maker() as db:yield db
    old=dict(app.dependency_overrides);app.dependency_overrides[get_db]=dependency;c=TestClient(app)
    monkeypatch.setattr(g.requests,'post',lambda *a,**k:pytest.fail('Unexpected provider call'))
    monkeypatch.setattr(g._jwks,'get_signing_key_from_jwt',lambda *a,**k:pytest.fail('Unexpected JWKS call'))
    yield SimpleNamespace(c=c,m=maker,uid=uid,origin={'Origin':'http://localhost:3000'})
    c.close();app.dependency_overrides.clear();app.dependency_overrides.update(old);engine.dispose()
def auth(e,ready=False):
    if ready:
        with e.m() as db:db.get(User,e.uid).must_change_password=False;db.commit()
    r=e.c.post('/api/auth/login',json=dict(username='creator',password=P));assert r.status_code==200
    return {'Authorization':'Bearer '+r.json()['access_token']}
def flow(e,mp,email='google@example.com',sub='google-sub',h=None):
    if h:
        r=e.c.post('/api/auth/google/link',headers={**h,**e.origin},json={'password':P});assert r.status_code==200;url=r.json()['authorization_url']
    else:
        r=e.c.get('/api/auth/google/start',follow_redirects=False);assert r.status_code==303;url=r.headers['location']
    q=parse_qs(urlparse(url).query);assert q['code_challenge_method']==['S256']
    with e.m() as db:a=db.scalar(select(OAuthAttempt).where(OAuthAttempt.state_digest==g.digest(q['state'][0])));verifier=a.verifier
    response=MagicMock();response.json.return_value={'id_token':'synthetic-token'}
    def exchange(*args,**kwargs):
        assert kwargs['data']['code_verifier']==verifier and kwargs['timeout']==(3,10) and kwargs['allow_redirects'] is False
        return response
    mp.setattr(g.requests,'post',exchange)
    mp.setattr(g,'verify_google_token',lambda t,n:(sub,email) if n==g.digest(q['nonce'][0]) else pytest.fail('Nonce mismatch'))
    r=e.c.get('/api/auth/google/callback',params=dict(state=q['state'][0],code='synthetic-code'),follow_redirects=False)
    assert r.headers['location']=='http://localhost:3000/auth/google'
    return q

def test_force_password_change_revokes_sessions_then_allows_creator_authoring(env):
    h=auth(env)
    assert env.c.get('/api/auth/me',headers=h).json()['must_change_password']
    assert env.c.get('/api/rooms',headers=h).status_code==403
    assert env.c.get('/api/vpn/profile',headers=h).status_code==403
    r=env.c.post('/api/auth/password',headers=h,json=dict(current_password=P,new_password=NEW));assert r.status_code==200
    assert env.c.get('/api/auth/me',headers=h).status_code==401
    h={'Authorization':'Bearer '+r.json()['access_token']}
    assert not env.c.get('/api/auth/me',headers=h).json()['must_change_password']
    assert env.c.post('/api/rooms',headers=h,json=dict(name='Authoring',category='linux',difficulty='easy')).status_code==201
    assert env.c.post('/api/auth/login',json=dict(username='OWNER@EXAMPLE.COM',password=NEW)).status_code==200
    assert env.c.post('/api/auth/login',json=dict(username='creator',password=P)).status_code==401

def test_logout_revokes_existing_tokens(env):
    h=auth(env,True);second=auth(env)
    assert env.c.post('/api/auth/logout',headers=h).status_code==204
    assert env.c.get('/api/auth/me',headers=second).status_code==401
@pytest.mark.parametrize('update',[dict(username='ab'),dict(email='invalid'),dict(password='short'),dict(role='creator'),dict(id=1)])
def test_registration_validation(env,update):
    assert env.c.post('/api/users',json={**dict(username='newlearner',email='new@example.com',password=NEW),**update}).status_code==422

def test_registration_collisions_normalization_and_learner_role(env):
    p=dict(username=' New_Learner ',email='NEW@EXAMPLE.COM',password=NEW)
    r=env.c.post('/api/users',json=p);assert r.status_code==201
    assert (r.json()['username'],r.json()['email'],r.json()['role'])==('new_learner','new@example.com','learner')
    assert 'password_hash' not in r.text and NEW not in r.text
    assert env.c.post('/api/users',json={**p,'username':'otheruser'}).status_code==409
    assert env.c.post('/api/users',json={**p,'email':'other@example.com'}).status_code==409
@pytest.mark.parametrize('old,new,code',[('wrong',NEW,401),(P,P,422),(P,'tiny',422),(P,'x'*129,422)])
def test_password_errors_do_not_invalidate_session(env,old,new,code):
    h=auth(env);assert env.c.post('/api/auth/password',headers=h,json=dict(current_password=old,new_password=new)).status_code==code
    assert env.c.get('/api/auth/me',headers=h).status_code==200

def test_google_signup_stable_sub_and_no_password(env,monkeypatch):
    flow(env,monkeypatch)
    assert env.c.get('/api/auth/google/status').json()==dict(choose_username=True,linking=False)
    r=env.c.post('/api/auth/google/complete',headers=env.origin,json=dict(username=' New_Google '));assert r.status_code==200
    h={'Authorization':'Bearer '+r.json()['access_token']};u=env.c.get('/api/auth/me',headers=h).json()
    assert u['role']=='learner' and not u['password_enabled'] and u['username']=='new_google'
    assert env.c.post('/api/auth/google/complete',headers=env.origin,json={}).status_code==401
    with env.m() as db:assert db.scalar(select(OAuthAttempt)).verifier is None
    flow(env,monkeypatch,email='changed@example.com')
    r=env.c.post('/api/auth/google/complete',headers=env.origin,json={})
    assert decode_access_token(r.json()['access_token'])['sub']==str(u['id'])
    assert env.c.post('/api/auth/login',json=dict(username='new_google',password=NEW)).status_code==401

def test_google_email_never_auto_links(env,monkeypatch):
    flow(env,monkeypatch,email='owner@example.com')
    assert env.c.post('/api/auth/google/complete',headers=env.origin,json=dict(username='othername')).status_code==409
    with env.m() as db:assert db.query(User).count()==1 and db.query(GoogleIdentity).count()==0

def test_google_username_retry_and_role_injection(env,monkeypatch):
    flow(env,monkeypatch)
    assert env.c.post('/api/auth/google/complete',headers=env.origin,json=dict(username='creator')).status_code==409
    assert env.c.post('/api/auth/google/complete',headers=env.origin,json=dict(username='newgoogle',role='admin')).status_code==422
    assert env.c.post('/api/auth/google/complete',headers={'Origin':'https://evil.invalid'},json=dict(username='newgoogle')).status_code==403
    assert env.c.post('/api/auth/google/complete',headers=env.origin,json=dict(username='newgoogle')).status_code==200

def test_explicit_link_reauthentication_and_origin(env,monkeypatch):
    h=auth(env,True)
    assert env.c.post('/api/auth/google/link',headers=h,json=dict(password=P)).status_code==403
    assert env.c.post('/api/auth/google/link',headers={**h,**env.origin},json=dict(password='wrong')).status_code==401
    flow(env,monkeypatch,email='owner@example.com',h=h)
    r=env.c.post('/api/auth/google/complete',headers=env.origin,json={});assert r.status_code==200
    assert decode_access_token(r.json()['access_token'])['sub']==str(env.uid)
    with env.m() as db:assert db.query(User).count()==1
    assert env.c.get('/api/auth/methods',headers=h).json()['google_linked']

def test_link_cannot_complete_after_logout(env,monkeypatch):
    h=auth(env,True);flow(env,monkeypatch,h=h);env.c.post('/api/auth/logout',headers=h)
    assert env.c.post('/api/auth/google/complete',headers=env.origin,json={}).status_code==401
@pytest.mark.parametrize('kind',['wrong_state','wrong_browser','expired','cancelled','provider'])
def test_state_expiry_cancel_and_failure_are_safe(env,monkeypatch,kind):
    r=env.c.get('/api/auth/google/start',follow_redirects=False);assert 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=lax' in r.headers['set-cookie']
    state=parse_qs(urlparse(r.headers['location']).query)['state'][0];params=dict(state=state,code='secret-code')
    if kind=='wrong_state':params['state']='wrong'
    if kind=='wrong_browser':env.c.cookies.clear()
    if kind=='expired':
        with env.m() as db:db.scalar(select(OAuthAttempt)).expires_at=utc_now()-timedelta(seconds=1);db.commit()
    if kind=='cancelled':params['error']='access_denied'
    if kind=='provider':monkeypatch.setattr(g.requests,'post',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('secret-diagnostic')))
    r=env.c.get('/api/auth/google/callback',params=params,follow_redirects=False)
    expected='cancelled' if kind=='cancelled' else 'provider' if kind=='provider' else 'invalid'
    assert r.headers['location'].endswith('oauth='+expected) and 'secret' not in r.text
    if kind in ('cancelled','provider'):
        env.c.cookies.set(g.cookie_name(),'not-valid')
        assert env.c.get('/api/auth/google/callback',params=params,follow_redirects=False).headers['location'].endswith('oauth=invalid')

@pytest.fixture
def keys():return rsa.generate_private_key(public_exponent=65537,key_size=2048)
def claims():return dict(iss='https://accounts.google.com',aud='synthetic-client',sub='stable',iat=utc_now(),exp=utc_now()+timedelta(minutes=5),nonce='expected',email='google@example.com',email_verified=True)
@pytest.mark.parametrize('change',[dict(iss='evil'),dict(aud='wrong'),dict(exp=0),dict(nonce='wrong'),dict(email_verified=False),dict(azp='wrong'),dict(sub=''),dict(email='bad'),dict(aud=['synthetic-client','other'])])
def test_signed_claims_are_verified(env,monkeypatch,keys,change):
    token=jwt.encode({**claims(),**change},keys,algorithm='RS256')
    monkeypatch.setattr(g._jwks,'get_signing_key_from_jwt',lambda t:SimpleNamespace(key=keys.public_key()))
    with pytest.raises((ValueError,jwt.InvalidTokenError)):g.verify_google_token(token,g.digest('expected'))
def test_real_signature_and_algorithm_allowlist(env,monkeypatch,keys):
    monkeypatch.setattr(g._jwks,'get_signing_key_from_jwt',lambda t:SimpleNamespace(key=keys.public_key()))
    assert g.verify_google_token(jwt.encode(claims(),keys,algorithm='RS256'),g.digest('expected'))==('stable','google@example.com')
    other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    for token in [jwt.encode(claims(),other,algorithm='RS256'),jwt.encode(claims(),'synthetic-secret',algorithm='HS256')]:
        with pytest.raises(jwt.InvalidTokenError):g.verify_google_token(token,g.digest('expected'))
def test_callback_query_not_in_access_log():
    r=logging.LogRecord('uvicorn.access',20,'test',1,'%s "%s %s HTTP/%s" %d',('client','GET','/api/auth/google/callback?code=private&state=private','1.1',303),None)
    g.OAuthAccessLogFilter().filter(r);assert 'private' not in r.getMessage()
def test_migration_preserves_legacy_data_and_is_rerunnable():
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY,username VARCHAR(50) NOT NULL,email VARCHAR(255) NOT NULL,password_hash VARCHAR(255) NOT NULL,role VARCHAR(20) NOT NULL)'))
        db.execute(text("INSERT INTO users VALUES(1,'Legacy','legacy@example.com','unchanged-hash','creator')"))
        assert migrate(db) and not migrate(db)
        assert db.execute(text('SELECT username,password_hash,role,password_enabled,must_change_password,token_version FROM users')).one()==('Legacy','unchanged-hash','creator',1,0,0)
    engine.dispose()
def test_migration_refuses_email_collision_before_ddl():
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,email TEXT,password_hash TEXT,role TEXT)'))
        db.execute(text("INSERT INTO users VALUES(1,'a','same@example.com','hash','learner'),(2,'b','SAME@example.com','hash','learner')"))
        with pytest.raises(RuntimeError):migrate(db)
        assert 'token_version' not in {c['name'] for c in inspect(db).get_columns('users')}
    engine.dispose()

def test_invalid_password_not_echoed_in_validation_response(env):
    secret='tiny'
    r=env.c.post('/api/users',json=dict(username='newuser',email='new@example.com',password=secret))
    assert r.status_code==422 and secret not in r.text and 'input' not in r.text

def test_secure_cookie_in_production(env,monkeypatch):
    monkeypatch.setattr(settings,'production',True)
    r=env.c.get('/api/auth/google/start',follow_redirects=False)
    assert '__Host-secforge-oauth=' in r.headers['set-cookie'] and 'Secure' in r.headers['set-cookie']

def test_reserved_old_and_service_vpn_addresses_cannot_be_reassigned(env,monkeypatch):
    from app.vpn_routes import _candidate_ips
    monkeypatch.setattr(settings,'vpn_reserved_client_ips',['10.8.0.65','10.8.0.67'])
    with env.m() as db:
        candidates=_candidate_ips(db)
    assert '10.8.0.65' not in candidates and '10.8.0.67' not in candidates
    assert '10.8.0.66' in candidates
