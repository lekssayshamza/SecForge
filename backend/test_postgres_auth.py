"""Real row locks/uniqueness; only the harness-owned disposable PostgreSQL."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires owned disposable PostgreSQL runner',allow_module_level=True)
import threading
from datetime import timedelta
from types import SimpleNamespace
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select,text,inspect
from pydantic import SecretStr
from app.auth_migration import migrate
from app.auth import change_password
from app import google_auth as g
from app.models import User,OAuthAttempt,GoogleIdentity
from app.schemas import GoogleCompleteRequest,PasswordChangeRequest
from app.config import settings
from app.lab_time import utc_now
from app.security import hash_password
from test_postgres_lab_concurrency import pg_sessions

def run_workers(actor):
    barrier=threading.Barrier(2,timeout=10);results=[];errors=[]
    def worker():
        try:results.append(actor(barrier))
        except BaseException as error:errors.append(error)
    workers=[threading.Thread(target=worker) for _ in range(2)]
    for w in workers:w.start()
    for w in workers:w.join(30)
    assert not any(w.is_alive() for w in workers) and not errors
    return sorted(results)

def test_google_handoff_completes_once_with_row_locks(pg_sessions,monkeypatch):
    for k,v in dict(google_oauth_enabled=True,registration_enabled=True,app_public_url='http://test',production=False).items():monkeypatch.setattr(settings,k,v)
    cookie='browser-owned-test'
    with pg_sessions() as db:
        db.add(OAuthAttempt(state_digest=g.digest('state'),browser_digest=g.digest(cookie),nonce_digest=g.digest('nonce'),verifier=None,expires_at=utc_now()+timedelta(minutes=5),callback_consumed=True,completed=False,subject='stable-google-sub',email='google@example.com'));db.commit()
    request=Request({'type':'http','headers':[(b'origin',b'http://test'),(b'cookie',('secforge-oauth='+cookie).encode())]})
    def actor(barrier):
        with pg_sessions() as db:
            barrier.wait()
            try:g.complete(GoogleCompleteRequest(username='newgoogle'),request,db);return 200
            except HTTPException as e:return e.status_code
    assert run_workers(actor)==[200,401]
    with pg_sessions() as db:
        assert db.query(User).count()==1 and db.query(GoogleIdentity).count()==1
        assert db.scalar(select(User.role))=='learner'
        assert db.scalar(select(OAuthAttempt.completed))

def test_concurrent_password_changes_only_one_version_wins(pg_sessions):
    old='synthetic-original-password';new='synthetic-new-long-password'
    with pg_sessions() as db:
        u=User(username='creator',email='owner@example.com',password_hash=hash_password(old),role='creator',must_change_password=True);db.add(u);db.commit();uid=u.id
    def actor(barrier):
        with pg_sessions() as db:
            u=db.get(User,uid);barrier.wait()
            try:change_password(PasswordChangeRequest(current_password=old,new_password=new),u,db);return 200
            except HTTPException as e:return e.status_code
    assert run_workers(actor)==[200,409]
    with pg_sessions() as db:
        u=db.get(User,uid);assert u.token_version==1 and not u.must_change_password

def test_auth_migration_is_additive_idempotent_and_enforces_email_uniqueness(pg_sessions):
    with pg_sessions() as db:
        db.execute(text('DROP TABLE oauth_attempts'));db.execute(text('DROP TABLE google_identities'))
        db.execute(text('DROP INDEX uq_users_email_ci'))
        for c in ['password_enabled','must_change_password','token_version']:db.execute(text(f'ALTER TABLE users DROP COLUMN {c}'))
        db.execute(text("INSERT INTO users(username,email,password_hash,role,created_at) VALUES('legacy','legacy@example.com','unchanged-hash','creator',now())"));db.commit()
        assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        assert db.execute(text('SELECT username,password_hash,role,password_enabled,must_change_password,token_version FROM users')).one()==('legacy','unchanged-hash','creator',True,False,0)
        assert inspect(db.connection()).has_table('oauth_attempts')
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):db.execute(text("INSERT INTO users(username,email,password_hash,role,created_at) VALUES('other','LEGACY@example.com','hash','learner',now())"))
        db.rollback()


def test_invitation_concurrent_redemption_has_exactly_one_privilege_grant(pg_sessions):
    import hashlib
    from app.creator import accept_creator_invitation
    from app.models import CreatorInvitation,AdminAudit
    from app.schemas import CreatorInvitationAccept
    token='synthetic-single-use-invitation'
    with pg_sessions() as db:
        users=[User(username=n,email=n+'@example.com',password_hash='unused',role='learner') for n in ['first','second']]
        db.add_all(users);db.flush();ids=[u.id for u in users]
        db.add(CreatorInvitation(token_hash=hashlib.sha256(token.encode()).hexdigest(),invited_by=999,expires_at=utc_now().replace(tzinfo=None)+timedelta(days=1)));db.commit()
    import itertools
    counter=itertools.count()
    def actor(barrier):
        uid=ids[next(counter)]
        with pg_sessions() as db:
            u=db.get(User,uid);barrier.wait()
            try:accept_creator_invitation(CreatorInvitationAccept(token=token),u,db);return 200
            except HTTPException as exc:return exc.status_code
    assert run_workers(actor)==[200,409]
    with pg_sessions() as db:
        assert sorted((u.role,u.token_version) for u in db.scalars(select(User)))==[('creator',1),('learner',0)]
        assert db.query(AdminAudit).count()==1 and db.scalar(select(CreatorInvitation.accepted_at)) is not None
