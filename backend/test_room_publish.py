#!/usr/bin/env python3
"""Tests for the creator room publish/unpublish lifecycle."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Room, User
from app.security import create_access_token

client = TestClient(app)


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env():
    suffix = uuid4().hex[:8]
    session = SessionLocal()

    creator = User(
        username=f"pub_creator_{suffix}",
        email=f"pub_creator_{suffix}@example.com",
        password_hash="not-used",
        role="creator",
    )
    other_creator = User(
        username=f"pub_other_{suffix}",
        email=f"pub_other_{suffix}@example.com",
        password_hash="not-used",
        role="creator",
    )
    learner = User(
        username=f"pub_learner_{suffix}",
        email=f"pub_learner_{suffix}@example.com",
        password_hash="not-used",
        role="learner",
    )
    session.add_all([creator, other_creator, learner])
    session.flush()

    draft = Room(
        name=f"Publish Draft {suffix}",
        room_type="THEORY",
        category="Web",
        difficulty="easy",
        status="draft",
        creator_id=creator.id,
    )
    published = Room(
        name=f"Publish Published {suffix}",
        category="Web",
        difficulty="easy",
        status="published",
        creator_id=creator.id,
    )
    other_draft = Room(
        name=f"Publish Other Draft {suffix}",
        category="Web",
        difficulty="easy",
        status="draft",
        creator_id=other_creator.id,
    )
    session.add_all([draft, published, other_draft])
    session.commit()

    result = SimpleNamespace(
        creator_id=creator.id,
        other_creator_id=other_creator.id,
        learner_id=learner.id,
        draft_id=draft.id,
        published_id=published.id,
        other_draft_id=other_draft.id,
        creator_token=create_access_token(creator.id, creator.role),
        other_creator_token=create_access_token(other_creator.id, other_creator.role),
        learner_token=create_access_token(learner.id, learner.role),
    )
    session.close()
    return result


def publish(room_id: int, token: str):
    return client.post(f"/api/rooms/{room_id}/publish", headers=auth_header(token))


def unpublish(room_id: int, token: str):
    return client.post(f"/api/rooms/{room_id}/unpublish", headers=auth_header(token))


def get_status(room_id: int) -> str | None:
    session = SessionLocal()
    try:
        room = session.get(Room, room_id)
        return room.status if room else None
    finally:
        session.close()


def get_updated_at(room_id: int):
    session = SessionLocal()
    try:
        room = session.get(Room, room_id)
        return room.updated_at if room else None
    finally:
        session.close()


class TestPublish:
    def test_owner_can_publish_draft(self, env):
        before = get_updated_at(env.draft_id)
        response = publish(env.draft_id, env.creator_token)
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == env.draft_id
        assert data["status"] == "published"
        assert get_status(env.draft_id) == "published"
        assert get_updated_at(env.draft_id) > before

    def test_non_owner_cannot_publish(self, env):
        response = publish(env.draft_id, env.other_creator_token)
        assert response.status_code == 404
        assert get_status(env.draft_id) == "draft"

    def test_learner_cannot_publish(self, env):
        response = publish(env.draft_id, env.learner_token)
        assert response.status_code == 403
        assert get_status(env.draft_id) == "draft"

    def test_already_published_cannot_publish(self, env):
        response = publish(env.published_id, env.creator_token)
        assert response.status_code == 409
        assert get_status(env.published_id) == "published"

    def test_unauthenticated_cannot_publish(self, env):
        response = publish(env.draft_id, "not-a-real-token")
        assert response.status_code in (401, 403)
        assert get_status(env.draft_id) == "draft"


class TestUnpublish:
    def test_owner_can_unpublish_published(self, env):
        before = get_updated_at(env.published_id)
        response = unpublish(env.published_id, env.creator_token)
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == env.published_id
        assert data["status"] == "draft"
        assert get_status(env.published_id) == "draft"
        assert get_updated_at(env.published_id) > before

    def test_non_owner_cannot_unpublish(self, env):
        response = unpublish(env.published_id, env.other_creator_token)
        assert response.status_code == 404
        assert get_status(env.published_id) == "published"

    def test_learner_cannot_unpublish(self, env):
        response = unpublish(env.published_id, env.learner_token)
        assert response.status_code == 403
        assert get_status(env.published_id) == "published"

    def test_draft_cannot_unpublish(self, env):
        response = unpublish(env.draft_id, env.creator_token)
        assert response.status_code == 409
        assert get_status(env.draft_id) == "draft"


class TestLifecycleIntegration:
    def test_full_publish_unpublish_cycle(self, env):
        assert publish(env.draft_id, env.creator_token).status_code == 200
        assert get_status(env.draft_id) == "published"

        listed = client.get("/api/rooms", headers=auth_header(env.learner_token))
        assert listed.status_code == 200
        assert env.draft_id in [room["id"] for room in listed.json()]

        assert unpublish(env.draft_id, env.creator_token).status_code == 200
        assert get_status(env.draft_id) == "draft"

        listed = client.get("/api/rooms", headers=auth_header(env.learner_token))
        assert listed.status_code == 200
        assert env.draft_id not in [room["id"] for room in listed.json()]

    def test_unpublished_room_editable_again(self, env):
        assert unpublish(env.published_id, env.creator_token).status_code == 200

        response = client.patch(
            f"/api/rooms/{env.published_id}",
            json={"name": "Renamed After Unpublish"},
            headers=auth_header(env.creator_token),
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed After Unpublish"

    def test_published_room_still_locked_against_edit(self, env):
        assert publish(env.draft_id, env.creator_token).status_code == 200
        response = client.patch(
            f"/api/rooms/{env.draft_id}",
            json={"name": "Should Fail"},
            headers=auth_header(env.creator_token),
        )
        assert response.status_code == 409
