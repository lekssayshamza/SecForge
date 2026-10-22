"""Offline production guards; no persistent database or AWS access."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from app.config import Settings, settings
from app.auth import login, get_authenticated_user
from app.users import create_user
from app.schemas import LoginRequest, UserCreate
from app.lab_manager import LabManager
from app.models import BuilderEnvironment


def production(**updates):
    config = dict(_env_file=None, production=True, jwt_secret="new-production-key-" * 3,
        challenge_answer_key="preserved-answer-key", registration_enabled=False,
        private_beta_user_ids=[6, 459], trusted_hosts=["secforgeacademy.duckdns.org", "127.0.0.1"],
        cors_origins=["https://secforgeacademy.duckdns.org"])
    config.update(updates)
    return Settings(**config)


@pytest.mark.parametrize("update", [dict(jwt_secret="short"), dict(challenge_answer_key=""),
    dict(trusted_hosts=["*"]), dict(cors_origins=["http://example.test"]),
    dict(registration_enabled=True), dict(private_beta_user_ids=[])])
def test_production_fails_closed(update):
    with pytest.raises(ValueError):
        production(**update)


def test_development_defaults_remain_compatible():
    assert Settings(_env_file=None, jwt_secret="development-test-key").registration_enabled
    assert production().reconciliation_enabled


def test_closed_registration_does_not_touch_database(monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", False)
    db = MagicMock()
    with pytest.raises(HTTPException) as error:
        create_user(UserCreate(username="newlearner", email="new@example.com", password="test-password"), db)
    assert error.value.status_code == 403
    assert not db.mock_calls


@pytest.mark.parametrize("allowed", [True, False])
def test_beta_login_and_current_user_keep_id_principal(monkeypatch, allowed):
    monkeypatch.setattr(settings, "production", True)
    monkeypatch.setattr(settings, "private_beta_user_ids", [6, 459])
    user = SimpleNamespace(id=459 if allowed else 500, role="learner", password_hash="hash", password_enabled=True, token_version=0, suspended=False)
    db = MagicMock(); db.scalar.return_value = user; db.get.return_value = user
    with patch("app.auth.verify_password", return_value=True), patch("app.auth.create_access_token", return_value="opaque") as issue:
        if allowed:
            assert login(LoginRequest(username="learner", password="test-password"), db)["access_token"] == "opaque"
            issue.assert_called_once_with(user_id=459, role="learner", token_version=0)
        else:
            with pytest.raises(HTTPException) as error:
                login(LoginRequest(username="learner", password="test-password"), db)
            assert error.value.status_code == 401
            issue.assert_not_called()
    with patch("app.auth.decode_access_token", return_value={"sub": str(user.id)}):
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="opaque")
        if allowed:
            assert get_authenticated_user(credentials, db) is user
        else:
            with pytest.raises(HTTPException) as error:
                get_authenticated_user(credentials, db)
            assert error.value.status_code == 403


def test_preparation_cannot_start_worker(monkeypatch):
    from app.reconciliation import reconciliation_lifespan
    monkeypatch.setattr(settings, "reconciliation_enabled", False)
    async def check():
        with patch("app.reconciliation.ReconciliationWorker") as worker:
            async with reconciliation_lifespan(MagicMock()):
                pass
            worker.assert_not_called()
    asyncio.run(check())


def test_generated_builder_management_ingress_is_opt_in():
    manager = LabManager()
    builder = BuilderEnvironment(id=99, creator_id=6, room_id=1, status="pending")
    manager.config["builder_management_security_group_id"] = ""
    assert "management_security_group_id" not in manager._generate_tfvars(builder)
    assert 'backend "s3"' in manager._generate_backend_config(builder)
    manager.config["builder_management_security_group_id"] = "sg-0123456789abcdef0"
    assert 'management_security_group_id = "sg-0123456789abcdef0"' in manager._generate_tfvars(builder)


def test_terraform_children_do_not_inherit_answer_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CHALLENGE_ANSWER_KEY", "private-test-key")
    manager = LabManager(); manager.config["terraform_plugin_cache_dir"] = str(tmp_path)
    assert "CHALLENGE_ANSWER_KEY" not in manager._get_safe_env()
