#!/usr/bin/env python3
"""Tests for the learner lab instance API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base, SessionLocal, get_db
from app.lab_manager import LabManager
from app.labs import ACTIVE_STATUSES
from app.main import app
from app.models import LabInstance, MachineTemplate, Room, RoomTemplate, User
from app.security import create_access_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "terraform_dry_run", True)
    monkeypatch.setattr(
        settings, "lab_terraform_workspace_root", str(tmp_path / "labs")
    )
    monkeypatch.setattr(settings, "builder_terraform_workspace_root", str(tmp_path / "builders"))

EXPECTED_LAB_KEYS = {
    "session_started_at", "extensions_used", "expired_at", "server_time",
    "id",
    "room_id",
    "status",
    "private_ip",
    "instance_id",
    "security_group_id",
    "expires_at",
    "created_at",
    "updated_at",
    "stopped_at",
    "error_message",
}


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def start_lab(room_id: int, token: str, ttl_hours: int = 4):
    return client.post(
        f"/api/rooms/{room_id}/lab",
        json={"ttl_hours": ttl_hours},
        headers=auth_header(token),
    )


def get_lab(room_id: int, token: str):
    return client.get(f"/api/rooms/{room_id}/lab", headers=auth_header(token))


def stop_lab(room_id: int, token: str):
    return client.post(f"/api/rooms/{room_id}/lab/stop", headers=auth_header(token))


def add_instance(room_id: int, learner_id: int, lab_status: str) -> int:
    session = SessionLocal()
    try:
        lab = LabInstance(
            room_id=room_id,
            learner_id=learner_id,
            status=lab_status,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        session.add(lab)
        session.commit()
        return lab.id
    finally:
        session.close()


def set_status(lab_id: int, lab_status: str) -> None:
    session = SessionLocal()
    try:
        lab = session.get(LabInstance, lab_id)
        lab.status = lab_status
        session.commit()
    finally:
        session.close()


def get_row(lab_id: int) -> LabInstance | None:
    session = SessionLocal()
    try:
        lab = session.get(LabInstance, lab_id)
        if lab is not None:
            session.expunge(lab)
        return lab
    finally:
        session.close()


def count_active(room_id: int, learner_id: int) -> int:
    session = SessionLocal()
    try:
        return len(
            session.scalars(
                select(LabInstance).where(
                    LabInstance.room_id == room_id,
                    LabInstance.learner_id == learner_id,
                    LabInstance.status.in_(ACTIVE_STATUSES),
                )
            ).all()
        )
    finally:
        session.close()


@pytest.fixture
def env():
    suffix = uuid4().hex[:8]
    session = SessionLocal()

    creator = User(
        username=f"lab_api_creator_{suffix}",
        email=f"lab_api_creator_{suffix}@example.com",
        password_hash="not-used",
        role="creator",
    )
    learner = User(
        username=f"lab_api_learner_{suffix}",
        email=f"lab_api_learner_{suffix}@example.com",
        password_hash="not-used",
        role="learner",
    )
    learner_two = User(
        username=f"lab_api_learner_two_{suffix}",
        email=f"lab_api_learner_two_{suffix}@example.com",
        password_hash="not-used",
        role="learner",
    )
    session.add_all([creator, learner, learner_two])
    session.flush()

    published = Room(
        name=f"API Lab Room {suffix}",
        category="Web",
        difficulty="easy",
        creator_id=creator.id,
        status="published",
    )
    draft = Room(
        name=f"API Draft Room {suffix}",
        category="Web",
        difficulty="easy",
        creator_id=creator.id,
        status="draft",
    )
    session.add_all([published, draft])
    session.flush()
    template = RoomTemplate(room_id=published.id, version=1, created_by=creator.id)
    session.add(template)
    session.flush()
    session.add(MachineTemplate(
        room_template_id=template.id, status="available", ami_id="ami-offline-challenge",
    ))
    session.commit()

    environment = SimpleNamespace(
        creator_id=creator.id,
        learner_id=learner.id,
        learner_two_id=learner_two.id,
        published_id=published.id,
        draft_id=draft.id,
        creator_token=create_access_token(creator.id, creator.role),
        learner_token=create_access_token(learner.id, learner.role),
        learner_two_token=create_access_token(learner_two.id, learner_two.role),
    )

    try:
        yield environment
    finally:
        session.close()

        cleanup = SessionLocal()
        try:
            cleanup.execute(
                delete(LabInstance)
                .where(LabInstance.room_id.in_([published.id, draft.id]))
                .execution_options(synchronize_session=False)
            )
            cleanup.execute(
                delete(LabInstance)
                .where(
                    LabInstance.learner_id.in_(
                        [creator.id, learner.id, learner_two.id]
                    )
                )
                .execution_options(synchronize_session=False)
            )
            template_ids = select(RoomTemplate.id).where(
                RoomTemplate.room_id.in_([published.id, draft.id])
            )
            cleanup.execute(delete(MachineTemplate).where(MachineTemplate.room_template_id.in_(template_ids)))
            cleanup.execute(delete(RoomTemplate).where(RoomTemplate.room_id.in_([published.id, draft.id])))
            cleanup.execute(
                delete(Room)
                .where(Room.id.in_([published.id, draft.id]))
                .execution_options(synchronize_session=False)
            )
            cleanup.execute(
                delete(User)
                .where(
                    User.id.in_([creator.id, learner.id, learner_two.id])
                )
                .execution_options(synchronize_session=False)
            )
            cleanup.commit()
        finally:
            cleanup.close()


def test_learner_can_start_lab_for_published_room(env):
    response = start_lab(env.published_id, env.learner_token)

    assert response.status_code == 201
    data = response.json()
    assert data["room_id"] == env.published_id
    assert data["status"] == "pending"
    assert data["instance_id"] is None
    assert data["private_ip"] is None
    assert data["security_group_id"] is None
    assert data["stopped_at"] is None

    expires_at = datetime.fromisoformat(data["expires_at"])
    ttl_seconds = (expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 3600 - 60 <= ttl_seconds <= 3600 + 60

    row = get_row(data["id"])
    assert row is not None
    assert row.learner_id == env.learner_id
    assert row.room_id == env.published_id
    assert row.status == "running"
    assert row.instance_id is not None
    assert row.private_ip is not None
    assert row.terraform_state_path is not None


def test_learner_cannot_start_lab_for_unpublished_room(env):
    response = start_lab(env.draft_id, env.learner_token)

    assert response.status_code == 409
    assert response.json()["detail"] == "Room is not published"
    assert count_active(env.draft_id, env.learner_id) == 0


def test_learner_cannot_start_lab_for_nonexistent_room(env):
    missing_room_id = env.published_id + 100000
    response = start_lab(missing_room_id, env.learner_token)

    assert response.status_code == 404
    assert response.json()["detail"] == "Room not found"


def test_active_duplicate_lab_is_rejected(env):
    first = start_lab(env.published_id, env.learner_token)
    second = start_lab(env.published_id, env.learner_token)

    assert first.status_code == 201
    assert second.status_code == 409
    assert (
        second.json()["detail"]
        == "Learner already has an active lab instance for this room"
    )
    assert count_active(env.published_id, env.learner_id) == 1


def test_stopped_and_failed_history_does_not_block_new_lab(env):
    stopped_id = add_instance(env.published_id, env.learner_id, "stopped")
    failed_id = add_instance(env.published_id, env.learner_id, "failed")

    response = start_lab(env.published_id, env.learner_token)

    assert response.status_code == 201
    assert response.json()["id"] not in (stopped_id, failed_id)
    assert get_row(stopped_id).status == "stopped"
    assert get_row(failed_id).status == "failed"
    assert count_active(env.published_id, env.learner_id) == 1


def test_learner_can_retrieve_own_lab(env):
    started = start_lab(env.published_id, env.learner_token)
    assert started.status_code == 201

    response = get_lab(env.published_id, env.learner_token)

    assert response.status_code == 200
    assert response.json()["id"] == started.json()["id"]
    assert response.json()["status"] == "running"


def test_learner_cannot_retrieve_another_learners_lab(env):
    started = start_lab(env.published_id, env.learner_token)
    assert started.status_code == 201

    own_response = get_lab(env.published_id, env.learner_token)
    other_response = get_lab(env.published_id, env.learner_two_token)

    assert own_response.status_code == 200
    assert other_response.status_code == 404
    assert other_response.json()["detail"] == "Lab instance not found for this room"
    assert own_response.json()["id"] == started.json()["id"]


def test_get_lab_returns_404_when_no_instance_exists(env):
    response = get_lab(env.published_id, env.learner_token)

    assert response.status_code == 404
    assert response.json()["detail"] == "Lab instance not found for this room"


def test_get_lab_prefers_active_instance_over_recent_stopped(env):
    active_id = add_instance(env.published_id, env.learner_id, "running")
    stopped_id = add_instance(env.published_id, env.learner_id, "stopped")

    response = get_lab(env.published_id, env.learner_token)

    assert response.status_code == 200
    assert response.json()["id"] == active_id
    assert get_row(stopped_id).status == "stopped"


def test_get_lab_returns_most_recent_when_no_active_instance(env):
    older_id = add_instance(env.published_id, env.learner_id, "stopped")
    newer_id = add_instance(env.published_id, env.learner_id, "failed")

    response = get_lab(env.published_id, env.learner_token)

    assert response.status_code == 200
    assert response.json()["id"] == newer_id
    assert get_row(older_id).status == "stopped"


def test_learner_can_stop_own_active_lab(env):
    started = start_lab(env.published_id, env.learner_token)
    assert started.status_code == 201

    response = stop_lab(env.published_id, env.learner_token)

    assert response.status_code == 200
    assert response.json()["id"] == started.json()["id"]
    assert response.json()["status"] == "stopping"
    assert get_row(started.json()["id"]).status == "stopped"


def test_learner_cannot_stop_another_learners_lab(env):
    started = start_lab(env.published_id, env.learner_token)
    assert started.status_code == 201

    response = stop_lab(env.published_id, env.learner_two_token)

    assert response.status_code == 404
    assert get_row(started.json()["id"]).status == "running"


@pytest.mark.parametrize("lab_status", ["pending", "provisioning", "running"])
def test_stoppable_statuses_transition_to_stopping(env, lab_status):
    lab_id = add_instance(env.published_id, env.learner_id, lab_status)

    response = stop_lab(env.published_id, env.learner_token)

    assert response.status_code == 200
    assert response.json()["id"] == lab_id
    assert response.json()["status"] == "stopping"
    assert get_row(lab_id).status == "stopped"


def test_stop_already_stopped_lab_is_idempotent(env):
    lab_id = add_instance(env.published_id, env.learner_id, "stopped")

    response = stop_lab(env.published_id, env.learner_token)

    assert response.status_code == 200
    assert response.json()["id"] == lab_id
    assert response.json()["status"] == "stopped"
    assert get_row(lab_id).status == "stopped"


def test_ttl_validation_rejects_values_outside_range(env):
    for ttl_hours in (0, 5, -1, 100):
        response = start_lab(env.published_id, env.learner_token, ttl_hours)

        assert response.status_code == 400
        assert response.json()["detail"] == "TTL must be between 1 and 4 hours"

    assert count_active(env.published_id, env.learner_id) == 0


def test_legacy_ttl_inputs_cannot_bypass_fixed_learner_hour(env):
    minimum = start_lab(env.published_id, env.learner_token, ttl_hours=1)
    assert minimum.status_code == 201
    minimum_ttl = (
        datetime.fromisoformat(minimum.json()["expires_at"]) - datetime.now(timezone.utc)
    ).total_seconds()
    assert 3600 - 60 <= minimum_ttl <= 3600 + 60

    maximum = start_lab(env.published_id, env.learner_two_token, ttl_hours=4)
    assert maximum.status_code == 201
    maximum_ttl = (
        datetime.fromisoformat(maximum.json()["expires_at"]) - datetime.now(timezone.utc)
    ).total_seconds()
    assert 3600 - 60 <= maximum_ttl <= 3600 + 60


def test_lab_response_does_not_expose_terraform_state_path(env):
    started = start_lab(env.published_id, env.learner_token)
    assert started.status_code == 201

    start_data = started.json()
    get_data = get_lab(env.published_id, env.learner_token).json()
    list_data = client.get("/api/labs/me", headers=auth_header(env.learner_token)).json()

    assert "terraform_state_path" not in start_data
    assert "terraform_state_path" not in get_data
    assert set(start_data) == EXPECTED_LAB_KEYS
    assert set(get_data) == EXPECTED_LAB_KEYS
    assert len(list_data) == 1
    assert set(list_data[0]) == EXPECTED_LAB_KEYS
    assert all("terraform_state_path" not in item for item in list_data)


def test_list_my_labs_returns_only_own_instances(env):
    own_started = start_lab(env.published_id, env.learner_token)
    assert own_started.status_code == 201
    own_historical = add_instance(env.published_id, env.learner_id, "stopped")
    other_lab = add_instance(env.published_id, env.learner_two_id, "running")

    response = client.get("/api/labs/me", headers=auth_header(env.learner_token))

    assert response.status_code == 200
    returned_ids = [item["id"] for item in response.json()]
    assert sorted(returned_ids) == sorted([own_started.json()["id"], own_historical])
    assert other_lab not in returned_ids

    other_response = client.get(
        "/api/labs/me", headers=auth_header(env.learner_two_token)
    )
    assert other_response.status_code == 200
    assert [item["id"] for item in other_response.json()] == [other_lab]


def test_owner_uses_creator_test_flow_without_accessing_learning_session(env):
    paths = [
        ("POST", f"/api/rooms/{env.published_id}/lab", {"ttl_hours": 4}),
        ("GET", f"/api/rooms/{env.published_id}/lab", None),
        ("POST", f"/api/rooms/{env.published_id}/lab/stop", None),
        ("GET", "/api/labs/me", None),
    ]

    for method, path, body in paths:
        response = client.request(
            method,
            path,
            json=body,
            headers=auth_header(env.creator_token),
        )
        assert response.status_code == (200 if path == "/api/labs/me" else 403 if path.endswith("/lab") and method == "POST" else 404), (method, path, response.status_code)

    assert count_active(env.published_id, env.creator_id) == 0


def test_unauthenticated_requests_are_rejected(env):
    responses = [
        client.post(f"/api/rooms/{env.published_id}/lab", json={"ttl_hours": 4}),
        client.get(f"/api/rooms/{env.published_id}/lab"),
        client.post(f"/api/rooms/{env.published_id}/lab/stop"),
        client.get("/api/labs/me"),
    ]

    for response in responses:
        assert response.status_code in (401, 403)


def test_concurrent_duplicate_starts_create_single_active_instance(tmp_path, monkeypatch):
    # Concurrent transactions need separate connections. StaticPool's shared
    # in-memory connection lets one request roll back the other request's insert.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-test.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        creator = User(username="concurrent_creator", email="creator@example.test",
                       password_hash="unused", role="creator")
        learner = User(username="concurrent_learner", email="learner@example.test",
                       password_hash="unused", role="learner")
        db.add_all([creator, learner])
        db.flush()
        room = Room(creator_id=creator.id, name="Concurrent test", category="web",
                    difficulty="easy", status="published")
        db.add(room)
        db.flush()
        template = RoomTemplate(room_id=room.id, version=1, created_by=creator.id)
        db.add(template)
        db.flush()
        db.add(MachineTemplate(room_template_id=template.id, status="available",
                               ami_id="ami-offline-challenge"))
        db.commit()
        room_id, learner_id = room.id, learner.id
        token = create_access_token(learner_id, "learner")

    def get_concurrent_db():
        with factory() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, get_concurrent_db)
    monkeypatch.setattr("app.labs.SessionLocal", factory)

    def attempt(_):
        concurrent_client = TestClient(app)
        return concurrent_client.post(
            f"/api/rooms/{room_id}/lab",
            json={"ttl_hours": 4},
            headers=auth_header(token),
        ).status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            status_codes = list(pool.map(attempt, range(2)))
        assert sorted(status_codes) == [201, 409]
        with Session(engine) as db:
            active = db.scalars(select(LabInstance).where(
                LabInstance.room_id == room_id, LabInstance.learner_id == learner_id,
                LabInstance.status.in_(ACTIVE_STATUSES),
            )).all()
            assert len(active) == 1
    finally:
        engine.dispose()


def test_database_rejects_second_active_instance_without_application_check(env):
    first = LabInstance(
        room_id=env.published_id,
        learner_id=env.learner_id,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    second = LabInstance(
        room_id=env.published_id,
        learner_id=env.learner_id,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )

    writer = SessionLocal()
    try:
        writer.add(first)
        writer.commit()
    finally:
        writer.close()

    racer = SessionLocal()
    try:
        racer.add(second)
        with pytest.raises(IntegrityError):
            racer.commit()
        racer.rollback()
    finally:
        racer.close()

    assert count_active(env.published_id, env.learner_id) == 1


def test_start_lab_hands_off_to_orchestration(env, monkeypatch):
    calls = []

    def fake_start(lab_manager_self, lab, db, template_id, image_id):
        calls.append(lab.id)

    monkeypatch.setattr(LabManager, "start_lab_instance", fake_start)

    response = start_lab(env.published_id, env.learner_token)

    assert response.status_code == 201
    lab_id = response.json()["id"]
    assert response.json()["status"] == "pending"
    assert calls == [lab_id]
    assert get_row(lab_id).status == "pending"


def test_stop_lab_hands_off_to_orchestration(env, monkeypatch):
    lab_id = add_instance(env.published_id, env.learner_id, "running")

    calls = []

    def fake_stop(lab_manager_self, lab, db):
        calls.append(lab.id)

    monkeypatch.setattr(LabManager, "stop_lab_instance", fake_stop)

    response = stop_lab(env.published_id, env.learner_token)

    assert response.status_code == 200
    assert response.json()["status"] == "stopping"
    assert calls == [lab_id]
    assert get_row(lab_id).status == "stopping"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
