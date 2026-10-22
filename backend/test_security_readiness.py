"""High-confidence launch-hardening regressions; no production mutations."""
import pytest
from datetime import datetime,timedelta
import hashlib
from sqlalchemy import select,func
from app.models import CreatorInvitation,User,AdminAudit
from app.security import create_access_token
from test_operations import ops,env

def invitation(e,token='synthetic-single-use-invitation'):
    with e.maker() as db:
        row=CreatorInvitation(token_hash=hashlib.sha256(token.encode()).hexdigest(),invited_by=e.ids['admin'],expires_at=datetime.utcnow()+timedelta(days=1));db.add(row);db.commit()
    return token

def test_invitation_single_use_audited_revokes_old_session(ops):
    e=ops;token=invitation(e);old=e.header('learner')
    r=e.client.post('/api/creator/invitations/accept',headers=old,json={'token':token})
    assert r.status_code==200 and r.json()['reauthentication_required']
    assert token not in r.text
    assert e.client.get('/api/auth/me',headers=old).status_code==401
    with e.maker() as db:
        u=db.get(User,e.ids['learner']);assert u.role=='creator' and u.token_version==1 and not u.creator_access
        event=db.scalar(select(AdminAudit));assert event.action=='creator-invitation-accept' and token not in event.context
        assert db.scalar(select(CreatorInvitation.accepted_at)) is not None
    fresh={'Authorization':'Bearer '+create_access_token(e.ids['learner'],'creator',1)}
    assert e.client.post('/api/creator/invitations/accept',headers=fresh,json={'token':token}).status_code==409
    with e.maker() as db:assert db.scalar(select(func.count()).select_from(AdminAudit))==1

def test_invitation_validation_and_expiry(ops):
    e=ops;token=invitation(e)
    assert e.client.post('/api/creator/invitations/accept',headers=e.header('learner'),json={'token':'x'*257}).status_code==422
    assert e.client.post('/api/admin/invitations',headers=e.header('admin'),json={'expires_in_days':100000}).status_code==422
    assert e.client.post('/api/creator/invitations/accept',headers=e.header('owner'),json={'token':token}).status_code==403
    with e.maker() as db:db.scalar(select(CreatorInvitation)).expires_at=datetime.utcnow()-timedelta(seconds=1);db.commit()
    assert e.client.post('/api/creator/invitations/accept',headers=e.header('learner'),json={'token':token}).status_code==410

def test_login_diagnostics_do_not_log_supplied_identity_or_password(ops,caplog):
    r=ops.client.post('/api/auth/login',json={'username':'never-log-this-identity','password':'never-log-this-password'})
    assert r.status_code==401 and 'Password sign-in rejected' in caplog.text
    assert 'never-log-this' not in caplog.text


def test_development_seed_refuses_production_before_opening_database(monkeypatch):
    from app import seed_dev
    from app.config import settings
    import pytest
    monkeypatch.setattr(settings,'production',True)
    monkeypatch.setattr(seed_dev,'SessionLocal',lambda:pytest.fail('Must not open production database'))
    with pytest.raises(RuntimeError,match='disabled in production'):seed_dev.seed_dev_user()


def test_csp_has_no_inline_scripts_and_preserves_same_origin_oauth_navigation():
    from pathlib import Path
    root=Path(__file__).resolve().parent.parent
    caddy=(root/'backend/deploy/cloud/Caddyfile').read_text()
    assert "script-src 'self';" in caddy and "object-src 'none'" in caddy
    assert 'max_size 3MB' in caddy and 'header Cache-Control "no-store"' in caddy
    html=(root/'frontend/index.html').read_text()
    assert '<script>' not in html and 'onload=' not in html
    assert 'src="/theme-init.js"' in html



@pytest.mark.parametrize('failed_task',[None,'_task','_orphan_task','_expiration_task'])
def test_health_detects_dead_existing_worker_without_starting_another(monkeypatch,failed_task):
    from app import main,reconciliation
    from app.config import settings
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    monkeypatch.setattr(settings,'reconciliation_enabled',True)
    tasks={name:SimpleNamespace(done=lambda:False) for name in ['_task','_orphan_task','_expiration_task']}
    if failed_task:tasks[failed_task]=SimpleNamespace(done=lambda:True)
    monkeypatch.setattr(reconciliation,'_reconciliation_worker',SimpleNamespace(**tasks) if failed_task else None)
    with pytest.raises(HTTPException) as exc:main.health_check()
    assert exc.value.status_code==503


def test_health_checks_database_when_all_existing_worker_tasks_alive(monkeypatch):
    from app import main,reconciliation
    from app.config import settings
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    monkeypatch.setattr(settings,'reconciliation_enabled',True)
    worker=SimpleNamespace(**{name:SimpleNamespace(done=lambda:False) for name in ['_task','_orphan_task','_expiration_task']})
    monkeypatch.setattr(reconciliation,'_reconciliation_worker',worker)
    database=MagicMock();monkeypatch.setattr(main,'engine',database)
    assert main.health_check()=={'status':'ok','database':'connected'}
    database.connect.return_value.__enter__.return_value.execute.assert_called_once()
    assert reconciliation._reconciliation_worker is worker



def test_password_memory_work_is_bounded_and_recovers_after_overload(monkeypatch):
    import threading
    from app import security
    from fastapi import HTTPException
    from types import SimpleNamespace
    ready=threading.Barrier(3,timeout=5);release=threading.Event();results=[]
    def expensive(*args):
        ready.wait();assert release.wait(5);return True
    monkeypatch.setattr(security,'password_hasher',SimpleNamespace(verify=expensive))
    def run():results.append(security.verify_password('synthetic-password','synthetic-hash'))
    threads=[threading.Thread(target=run) for _ in range(2)]
    for thread in threads:thread.start()
    try:
        ready.wait()
        with pytest.raises(HTTPException) as error:security.verify_password('synthetic-password','synthetic-hash')
        assert error.value.status_code==503 and error.value.headers['Retry-After']=='2'
    finally:
        release.set()
        for thread in threads:thread.join(5)
    assert results==[True,True] and not any(t.is_alive() for t in threads)
    monkeypatch.setattr(security,'password_hasher',SimpleNamespace(verify=lambda *args:True,hash=lambda value:'synthetic-result'))
    assert security.verify_password('synthetic-password','synthetic-hash')
    assert security.hash_password('synthetic-password')=='synthetic-result'
