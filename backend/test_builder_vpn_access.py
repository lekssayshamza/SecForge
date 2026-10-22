#!/usr/bin/env python3
"""Builder lifecycle ↔ VPN authorization integration tests.

Gateway and Terraform are mocked; the nftables sync seam is recorded so we
can assert exactly when creator builder authorization is (re)applied.
"""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import builder as builder_module
from app.backend_routes import BackendRouteError
from app.database import SessionLocal
from app.lab_manager import TerraformError
from app.main import app
from app.models import BuilderEnvironment, Room, User
from app.security import create_access_token
from app.vpn_authz import VpnAuthorizationError

client = TestClient(app)


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env():
    suffix = uuid4().hex[:8]
    session = SessionLocal()
    creator = User(
        username=f"bvpn_creator_{suffix}",
        email=f"bvpn_creator_{suffix}@example.com",
        password_hash="not-used",
        role="creator",
    )
    other_creator = User(
        username=f"bvpn_other_{suffix}",
        email=f"bvpn_other_{suffix}@example.com",
        password_hash="not-used",
        role="creator",
    )
    learner = User(
        username=f"bvpn_learner_{suffix}",
        email=f"bvpn_learner_{suffix}@example.com",
        password_hash="not-used",
        role="learner",
    )
    session.add_all([creator, other_creator, learner])
    session.flush()
    room = Room(
        name=f"Builder VPN Room {suffix}",
        category="web",
        difficulty="easy",
        status="draft",
        creator_id=creator.id,
    )
    other_room = Room(
        name=f"Builder VPN Other {suffix}",
        category="web",
        difficulty="easy",
        status="draft",
        creator_id=other_creator.id,
    )
    session.add_all([room, other_room])
    session.commit()
    result = SimpleNamespace(
        creator_id=creator.id,
        other_creator_id=other_creator.id,
        learner_id=learner.id,
        room_id=room.id,
        other_room_id=other_room.id,
        creator_token=create_access_token(creator.id, creator.role),
        other_creator_token=create_access_token(other_creator.id, other_creator.role),
        learner_token=create_access_token(learner.id, learner.role),
    )
    session.close()
    return result


@pytest.fixture
def sync_recorder(monkeypatch):
    calls = []
    route_calls = []
    failure = {"raise": False}
    route_failure = {"raise": False}

    def _authorize(db, builder):
        if failure["raise"]:
            raise VpnAuthorizationError("VPN authorization update failed")
        calls.append(builder)

    def _sync(db):
        if failure["raise"]:
            raise VpnAuthorizationError("sync failed")
        calls.append("sync")

    def _routes(db):
        route_calls.append(len(route_calls) + 1)
        if route_failure["raise"]:
            raise BackendRouteError("route sync failed")
        return set(), set()

    monkeypatch.setattr(builder_module, "authorize_builder_access", _authorize)
    monkeypatch.setattr(builder_module, "sync_vpn_authz", _sync)
    monkeypatch.setattr(builder_module, "sync_backend_routes", _routes)
    return SimpleNamespace(
        calls=calls,
        route_calls=route_calls,
        failure=failure,
        route_failure=route_failure,
    )


@pytest.fixture
def fake_lab_manager(monkeypatch):
    def _start(builder):
        builder.status = "running"
        builder.instance_id = "i-0fakebuilder"
        builder.private_ip = "10.0.1.210"
        builder.security_group_id = "sg-0fakebuilder"

    def _stop(builder):
        builder.status = "stopped"
        builder.instance_id = None
        builder.private_ip = None
        builder.security_group_id = None
        builder.stopped_at = datetime.utcnow()

    manager = MagicMock()
    manager.start_builder.side_effect = _start
    manager.stop_builder.side_effect = _stop
    monkeypatch.setattr(builder_module, "lab_manager", manager)
    return manager


def start_builder(room_id: int, token: str, ttl_hours: int = 2):
    return client.post(
        f"/api/rooms/{room_id}/builder",
        json={"ttl_hours": ttl_hours},
        headers=auth_header(token),
    )


def stop_builder(room_id: int, token: str):
    return client.post(
        f"/api/rooms/{room_id}/builder/stop",
        headers=auth_header(token),
    )


def get_builder_row(room_id: int) -> BuilderEnvironment | None:
    session = SessionLocal()
    try:
        from sqlalchemy import select

        row = session.scalar(
            select(BuilderEnvironment).where(
                BuilderEnvironment.room_id == room_id
            )
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def delete_builder_rows(room_id: int) -> None:
    session = SessionLocal()
    try:
        from sqlalchemy import delete

        session.execute(
            delete(BuilderEnvironment).where(
                BuilderEnvironment.room_id == room_id
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def cleanup_builders(env):
    yield
    for room_id in (env.room_id, env.other_room_id):
        delete_builder_rows(room_id)


class TestBuilderStartSyncsAuthz:
    @pytest.mark.parametrize("persistent", [False, True])
    def test_builder_workspace_uses_configured_root(
        self, env, sync_recorder, fake_lab_manager, monkeypatch, tmp_path, persistent
    ):
        root = str(tmp_path / "persistent-builders") if persistent else "/tmp/secforge-builders"
        monkeypatch.setattr(builder_module.settings, "builder_terraform_workspace_root", root)
        response = start_builder(env.room_id, env.creator_token)
        assert response.status_code == 201
        row = get_builder_row(env.room_id)
        assert row.terraform_state_path == str(Path(root) / f"builder-{row.id}")
        assert Path(root) in Path(row.terraform_state_path).parents
        assert fake_lab_manager.start_builder.call_args.args[0].terraform_state_path == row.terraform_state_path

    def test_running_builder_triggers_sync(self, env, sync_recorder, fake_lab_manager):
        response = start_builder(env.room_id, env.creator_token)
        assert response.status_code == 201
        assert response.json()["status"] == "running"
        assert len(sync_recorder.calls) == 1
        # The builder-aware authorization is handed the in-memory builder whose
        # running state/private IP are not yet committed, so it can add the
        # not-yet-visible pair explicitly (the autoflush=False race regression).
        authorized = sync_recorder.calls[0]
        assert authorized.status == "running"
        assert authorized.private_ip == "10.0.1.210"
        assert authorized.instance_id == "i-0fakebuilder"
        # A running builder also gets its backend tunnel host route attempted.
        assert len(sync_recorder.route_calls) == 1
        assert get_builder_row(env.room_id).status == "running"

    def test_route_sync_failure_does_not_fail_builder(
        self, env, sync_recorder, fake_lab_manager
    ):
        sync_recorder.route_failure["raise"] = True
        response = start_builder(env.room_id, env.creator_token)
        assert response.status_code == 201
        assert response.json()["status"] == "running"
        assert len(sync_recorder.calls) == 1
        assert get_builder_row(env.room_id).status == "running"

    def test_sync_failure_marks_builder_failed(
        self, env, sync_recorder, fake_lab_manager
    ):
        sync_recorder.failure["raise"] = True
        response = start_builder(env.room_id, env.creator_token)
        assert response.status_code == 201
        assert response.json()["status"] == "failed"
        assert "VPN authorization" in response.json()["error_message"]
        assert get_builder_row(env.room_id).status == "failed"
        # A failed builder is not a running target; no host route is added.
        assert sync_recorder.route_calls == []

    def test_terraform_failure_does_not_sync(self, env, sync_recorder, monkeypatch):
        def _boom(builder):
            builder.status = "failed"
            builder.error_message = "Terraform apply failed"
            raise TerraformError("apply failed")

        monkeypatch.setattr(builder_module.lab_manager, "start_builder", _boom)
        response = start_builder(env.room_id, env.creator_token)
        assert response.status_code == 201
        assert response.json()["status"] == "failed"
        assert sync_recorder.calls == []

    def test_learner_cannot_start_builder(self, env, sync_recorder, fake_lab_manager):
        response = start_builder(env.room_id, env.learner_token)
        assert response.status_code == 403
        assert sync_recorder.calls == []

    def test_non_owner_cannot_start_builder(
        self, env, sync_recorder, fake_lab_manager
    ):
        response = start_builder(env.room_id, env.other_creator_token)
        assert response.status_code == 404
        assert sync_recorder.calls == []


class TestBuilderStopRemovesAuthz:
    def test_stop_triggers_sync(self, env, sync_recorder, fake_lab_manager):
        assert start_builder(env.room_id, env.creator_token).status_code == 201
        sync_recorder.calls.clear()

        response = stop_builder(env.room_id, env.creator_token)
        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
        # Sync runs after the builder is claimed as stopping so the
        # creator's pair is removed from the desired state immediately.
        assert len(sync_recorder.calls) == 1

    def test_sync_failure_during_stop_does_not_block(
        self, env, sync_recorder, fake_lab_manager
    ):
        assert start_builder(env.room_id, env.creator_token).status_code == 201
        sync_recorder.calls.clear()
        sync_recorder.failure["raise"] = True

        response = stop_builder(env.room_id, env.creator_token)
        assert response.status_code == 200
        assert response.json()["status"] == "stopped"

    def test_non_owner_cannot_stop_builder(
        self, env, sync_recorder, fake_lab_manager
    ):
        assert start_builder(env.room_id, env.creator_token).status_code == 201
        sync_recorder.calls.clear()
        response = stop_builder(env.room_id, env.other_creator_token)
        assert response.status_code == 404
        assert sync_recorder.calls == []


def test_builder_response_reports_configured_login_without_key_material(
    env, fake_lab_manager, sync_recorder, monkeypatch
):
    from app.config import settings
    monkeypatch.setattr(settings, "builder_ssh_user", "challenge_admin")
    monkeypatch.setattr(settings, "builder_ssh_key_path", "/synthetic/private/server-key.pem")
    response = start_builder(env.room_id, env.creator_token)
    assert response.status_code == 201
    assert response.json()["ssh_username"] == "challenge_admin"
    response = client.get(f"/api/rooms/{env.room_id}/builder", headers=auth_header(env.creator_token))
    assert response.status_code == 200
    assert response.json()["ssh_username"] == "challenge_admin"
    assert "server-key.pem" not in response.text
    assert "ssh_key_path" not in response.json()
    for token in (env.other_creator_token, env.learner_token):
        denied = client.get(f"/api/rooms/{env.room_id}/builder", headers=auth_header(token))
        assert denied.status_code in (403, 404)
        assert "ssh_username" not in denied.json()
