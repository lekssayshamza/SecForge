#!/usr/bin/env python3
"""Room template capture: endpoint authz/lifecycle, pinning, and AMI resolution."""

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import templates as templates_module
from app.ami_manager import (
    AMIError,
    AMITimeoutError,
    AmbiguousImageCreateError,
    ImageUnfreezeError,
)
from app.capture_identity import capture_image_tags
from app.config import settings
from app.database import Base, get_db
from app.lab_manager import LAB_FIRST_BOOT_USER_DATA, LabManager, TemplateResolutionError
from app.main import app
from app.models import (
    BuilderEnvironment,
    CaptureRequest,
    LabInstance,
    MachineTemplate,
    Room,
    RoomTemplate,
    User,
)
from app.ops_lock import builder_lock_key, single_flight
from app.security import create_access_token
from app.templates import resolve_lab_ami

client = TestClient(app)


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def override_db(engine):
    def _get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    yield
    app.dependency_overrides.clear()


class FakeAMIManager:
    def __init__(self, fail=False, timeout=False, fail_message="create image failed",
                 ambiguous=False, unfreeze_image_id=None):
        self.fail = fail
        self.timeout = timeout
        self.fail_message = fail_message
        self.ambiguous = ambiguous
        self.unfreeze_image_id = unfreeze_image_id
        self.created = []

    def create_image(
        self, instance_id, name, tags=None, no_reboot=True, client_token=None
    ):
        if self.fail:
            raise AMIError(self.fail_message)
        self.created.append(
            {
                "instance_id": instance_id,
                "name": name,
                "tags": tags,
                "client_token": client_token,
            }
        )
        if self.ambiguous:
            raise AmbiguousImageCreateError("create image outcome is unknown")
        if self.unfreeze_image_id is not None:
            raise ImageUnfreezeError(
                "Builder filesystem could not be unfrozen after image capture: boom",
                image_id=self.unfreeze_image_id,
            )
        return "ami-0test"

    def wait_until_available(self, image_id, timeout=None, poll_interval=None):
        if self.timeout:
            raise AMITimeoutError("still pending")

    def get_image_state(self, image_id):
        return "available"

    def deregister_image(self, image_id):
        pass


@pytest.fixture
def env(engine):
    suffix = uuid4().hex[:8]
    with Session(engine) as session:
        creator = User(
            username=f"tmpl_creator_{suffix}",
            email=f"tmpl_creator_{suffix}@example.com",
            password_hash="x",
            role="creator",
        )
        other = User(
            username=f"tmpl_other_{suffix}",
            email=f"tmpl_other_{suffix}@example.com",
            password_hash="x",
            role="creator",
        )
        learner = User(
            username=f"tmpl_learner_{suffix}",
            email=f"tmpl_learner_{suffix}@example.com",
            password_hash="x",
            role="learner",
        )
        session.add_all([creator, other, learner])
        session.flush()

        room = Room(
            creator_id=creator.id,
            name=f"Template Room {suffix}",
            category="web",
            difficulty="easy",
            status="draft",
        )
        session.add(room)
        session.commit()

        data = {
            "creator_id": creator.id,
            "other_id": other.id,
            "learner_id": learner.id,
            "room_id": room.id,
            "creator_token": create_access_token(creator.id, creator.role),
            "other_token": create_access_token(other.id, other.role),
            "learner_token": create_access_token(learner.id, learner.role),
        }
    return data


def add_builder(
    engine,
    room_id,
    creator_id,
    status="running",
    instance_id="i-0builder",
    expires_in_hours=4,
):
    with Session(engine) as session:
        builder = BuilderEnvironment(
            room_id=room_id,
            creator_id=creator_id,
            status=status,
            instance_id=instance_id,
            private_ip="10.0.1.174",
            security_group_id="sg-0builder",
            terraform_state_path=f"/tmp/secforge-builders/builder-{room_id}",
            expires_at=datetime.utcnow() + timedelta(hours=expires_in_hours),
        )
        session.add(builder)
        session.commit()
        return builder.id


def save_template(room_id, token, idempotency_key=None):
    headers = auth_header(token)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(f"/api/rooms/{room_id}/templates", headers=headers)


def list_templates(room_id, token):
    return client.get(f"/api/rooms/{room_id}/templates", headers=auth_header(token))


class TestSaveTemplateAuthz:
    def test_learner_forbidden(self, env, monkeypatch):
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        assert save_template(env["room_id"], env["learner_token"]).status_code == 403

    def test_non_owner_not_found(self, env, monkeypatch):
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        assert save_template(env["room_id"], env["other_token"]).status_code == 404

    def test_requires_running_builder(self, env, monkeypatch):
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        assert save_template(env["room_id"], env["creator_token"]).status_code == 409

    def test_pending_builder_rejected(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"], status="pending")
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        assert save_template(env["room_id"], env["creator_token"]).status_code == 409


class TestSaveTemplateLifecycle:
    def test_success_creates_available_version(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        response = save_template(env["room_id"], env["creator_token"])
        assert response.status_code == 201
        body = response.json()
        assert body["version"] == 1
        machine = body["machines"][0]
        assert machine["status"] == "available"
        assert machine["ami_id"] == "ami-0test"
        assert machine["role"] == "primary"
        assert fake.created[0]["instance_id"] == "i-0builder"

    def test_second_save_creates_new_immutable_version(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())

        first = save_template(env["room_id"], env["creator_token"]).json()
        second = save_template(env["room_id"], env["creator_token"]).json()

        assert first["version"] == 1
        assert second["version"] == 2
        assert first["id"] != second["id"]

    def test_ami_failure_marks_machine_failed(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager(fail=True))

        response = save_template(env["room_id"], env["creator_token"])
        assert response.status_code == 201
        assert response.json()["machines"][0]["status"] == "failed"

    def test_wait_timeout_leaves_creating_with_ami_id(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager(timeout=True))

        body = save_template(env["room_id"], env["creator_token"]).json()
        machine = body["machines"][0]
        assert machine["status"] == "creating"
        assert machine["ami_id"] == "ami-0test"


class TestSaveTemplateCaptureSafety:
    def test_failed_capture_details_are_sanitized_logged_and_owner_only(self, env, engine, monkeypatch, caplog):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager(
            fail=True, fail_message="Builder console identity missing password=never-log-this"))
        response = save_template(env["room_id"], env["creator_token"])
        machine = response.json()["machines"][0]
        assert response.status_code == 201
        assert machine["status"] == "failed" and machine["ami_id"] is None
        assert 'console identity missing' in machine["error_message"]
        assert 'never-log-this' not in response.text + caplog.text
        assert 'REDACTED' in machine["error_message"]
        assert f'room={env["room_id"]}' in caplog.text
        assert 'phase=create status=failed' in caplog.text
        assert list_templates(env["room_id"], env["learner_token"]).status_code in (403, 404)
        assert client.get(f'/api/rooms/{env["room_id"]}/templates').status_code == 401

    def test_expired_running_builder_rejected(self, env, engine, monkeypatch):
        add_builder(
            engine, env["room_id"], env["creator_id"], expires_in_hours=-1
        )
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        response = save_template(env["room_id"], env["creator_token"])

        assert response.status_code == 409
        assert "expired" in response.json()["detail"].lower()
        assert fake.created == []

    def test_capture_passes_legacy_internal_label(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        response = save_template(env["room_id"], env["creator_token"])

        assert response.status_code == 201
        assert fake.created[0]["client_token"] == (
            f"secforge-tmpl-{response.json()['id']}"
        )

    def test_failed_capture_persists_underlying_cause(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        cause = (
            "Failed to flush builder filesystem before image capture: "
            "Builder remote command failed: connection: SSH command timed out"
        )
        monkeypatch.setattr(
            templates_module,
            "ami_manager",
            FakeAMIManager(fail=True, fail_message=cause),
        )

        response = save_template(env["room_id"], env["creator_token"])

        assert response.status_code == 201
        machine = response.json()["machines"][0]
        assert machine["status"] == "failed"
        assert "connection" in machine["error_message"]
        assert "timed out" in machine["error_message"]


class TestListTemplates:
    def test_owner_lists_versions(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        save_template(env["room_id"], env["creator_token"])
        save_template(env["room_id"], env["creator_token"])

        response = list_templates(env["room_id"], env["creator_token"])
        assert response.status_code == 200
        versions = [t["version"] for t in response.json()]
        assert versions == [2, 1]

    def test_learner_forbidden(self, env, engine, monkeypatch):
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        assert list_templates(env["room_id"], env["learner_token"]).status_code == 403


class TestPublishWithTemplate:
    def test_pending_capture_blocks_new_intent_but_replays_existing_request(self,env,engine,monkeypatch):
        add_builder(engine,env['room_id'],env['creator_id'])
        manager=FakeAMIManager(timeout=True)
        monkeypatch.setattr(templates_module,'ami_manager',manager)
        first=save_template(env['room_id'],env['creator_token'],'capture-intent-first')
        assert first.status_code==201
        assert save_template(env['room_id'],env['creator_token'],'capture-intent-second').status_code==409
        replay=save_template(env['room_id'],env['creator_token'],'capture-intent-first')
        assert replay.status_code==201 and replay.json()['id']==first.json()['id']
        workspace=client.get(f'/api/rooms/{env["room_id"]}/workspace',headers=auth_header(env['creator_token'])).json()
        assert workspace['image_template_id']==first.json()['id'] and workspace['image_status']=='creating'

    def _publish(self, room_id, template_id, token):
        return client.post(
            f"/api/rooms/{room_id}/templates/{template_id}/publish",
            headers=auth_header(token),
        )

    def test_publish_pins_available_version(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        template = save_template(env["room_id"], env["creator_token"]).json()

        headers={"Authorization": "Bearer " + env["creator_token"]}
        workspace=client.get(f'/api/rooms/{env["room_id"]}/workspace',headers=headers).json()
        response=client.post(f'/api/rooms/{env["room_id"]}/workspace/publish',headers=headers,json={'revision':workspace['revision']})
        assert response.status_code == 200
        assert response.json()["room"]["status"] == "published"

        listed = list_templates(env["room_id"], env["creator_token"]).json()
        published = [t for t in listed if t["is_published"]]
        assert len(published) == 1
        assert published[0]["id"] == template["id"]

    def test_cannot_publish_unavailable_template(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager(timeout=True))
        template = save_template(env["room_id"], env["creator_token"]).json()

        response = self._publish(env["room_id"], template["id"], env["creator_token"])
        assert response.status_code == 409

    def test_learner_forbidden(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        monkeypatch.setattr(templates_module, "ami_manager", FakeAMIManager())
        template = save_template(env["room_id"], env["creator_token"]).json()

        assert self._publish(env["room_id"], template["id"], env["learner_token"]).status_code == 403


class TestCaptureIdempotency:
    @pytest.mark.parametrize("ambiguous", [False, True])
    def test_persisted_intent_replay_and_tag_recovery_offline(
        self, env, engine, db_session, monkeypatch, ambiguous
    ):
        """Exercise the handler directly, with no HTTP transport or AWS."""
        from app.reconciliation import ReconciliationWorker

        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager(ambiguous=ambiguous)
        original_create = fake.create_image

        def create_after_commit(*args, **kwargs):
            with Session(engine) as session:
                capture = session.scalar(select(CaptureRequest))
                assert capture is not None
                assert capture.room_template_id is not None
                assert kwargs["tags"]["CaptureRequestId"] == str(capture.id)
            return original_create(*args, **kwargs)

        monkeypatch.setattr(fake, "create_image", create_after_commit)
        monkeypatch.setattr(templates_module, "ami_manager", fake)
        creator = db_session.get(User, env["creator_id"])
        first = templates_module.save_room_template(
            env["room_id"], creator, db_session, idempotency_key="offline-intent"
        )
        replay = templates_module.save_room_template(
            env["room_id"], creator, db_session, idempotency_key="offline-intent"
        )
        assert replay.id == first.id
        assert len(fake.created) == 1
        assert len(db_session.scalars(select(CaptureRequest)).all()) == 1
        machine = db_session.scalar(
            select(MachineTemplate).where(MachineTemplate.room_template_id == first.id)
        )
        assert machine.status == ("unknown" if ambiguous else "available")
        if not ambiguous:
            return

        machine.updated_at = datetime.utcnow() - timedelta(minutes=60)
        db_session.commit()
        recovery = FakeRecoveryAMIManager()
        worker = ReconciliationWorker()
        worker._make_ami_manager = lambda: recovery
        for _ in range(2):
            asyncio.run(worker._handle_stale_creating_machines(db_session))
        db_session.refresh(machine)
        assert machine.status == "unknown"
        assert machine.ami_id is None
        assert recovery.create_calls == []
        assert recovery.find_calls[0] == fake.created[0]["tags"]
        assert recovery.find_calls[0] == capture_image_tags(
            room_id=env["room_id"], template_id=first.id, version=first.version,
            builder_id=builder_id,
            capture_id=db_session.scalar(select(CaptureRequest)).id,
        )
        recovery.existing_image = "ami-visible-after-timeout"
        asyncio.run(worker._handle_stale_creating_machines(db_session))
        db_session.refresh(machine)
        assert machine.status == "available"
        assert recovery.create_calls == []
        assert len(fake.created) == 1

    def test_replay_same_key_creates_one_capture(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        first = save_template(env["room_id"], env["creator_token"], "key-1")
        second = save_template(env["room_id"], env["creator_token"], "key-1")

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["version"] == second.json()["version"] == 1
        assert len(fake.created) == 1

    def test_distinct_keys_get_distinct_versions_and_labels(
        self, env, engine, monkeypatch
    ):
        add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        first = save_template(env["room_id"], env["creator_token"], "key-a").json()
        second = save_template(env["room_id"], env["creator_token"], "key-b").json()

        assert first["id"] != second["id"]
        assert first["version"] == 1
        assert second["version"] == 2
        tokens = [call["client_token"] for call in fake.created]
        assert tokens == [
            f"secforge-tmpl-{first['id']}",
            f"secforge-tmpl-{second['id']}",
        ]
        assert tokens[0] != tokens[1]

    def test_absent_key_is_a_distinct_intent(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        first = save_template(env["room_id"], env["creator_token"]).json()
        second = save_template(env["room_id"], env["creator_token"]).json()

        assert first["id"] != second["id"]
        assert first["version"] == 1
        assert second["version"] == 2
        assert len(fake.created) == 2

    def test_same_key_resumes_active_capture_when_guard_contended(
        self, env, engine, monkeypatch
    ):
        # A retry of the same capture that races the in-flight one (guard held,
        # capture committed after the first lookup) must resume that capture,
        # not 409 or start a second CreateImage.
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        with Session(engine) as session:
            template = RoomTemplate(
                room_id=env["room_id"], version=1, created_by=env["creator_id"]
            )
            session.add(template)
            session.flush()
            session.add(
                CaptureRequest(
                    room_id=env["room_id"],
                    creator_id=env["creator_id"],
                    idempotency_key="retry-key",
                    room_template_id=template.id,
                )
            )
            session.add(
                MachineTemplate(
                    room_template_id=template.id, role="primary", status="creating"
                )
            )
            session.commit()
            template_id = template.id

        real_find = templates_module._find_capture_request
        calls = {"n": 0}

        def miss_once(db, room_id, creator_id, key):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_find(db, room_id, creator_id, key)

        monkeypatch.setattr(templates_module, "_find_capture_request", miss_once)

        with single_flight(builder_lock_key(builder_id)):
            response = save_template(env["room_id"], env["creator_token"], "retry-key")

        assert response.status_code == 201
        assert response.json()["id"] == template_id
        assert fake.created == []


class TestCaptureAmbiguity:
    def test_ambiguous_create_not_retried_and_recoverable(
        self, env, engine, monkeypatch
    ):
        add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager(ambiguous=True)
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        response = save_template(env["room_id"], env["creator_token"], "amb-1")

        assert response.status_code == 201
        machine = response.json()["machines"][0]
        assert machine["status"] == "unknown"
        assert machine["ami_id"] is None
        # The ambiguous outcome must not trigger an immediate second create.
        assert len(fake.created) == 1

    def test_unfreeze_failure_preserves_image_id(self, env, engine, monkeypatch):
        add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager(unfreeze_image_id="ami-0partial")
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        response = save_template(env["room_id"], env["creator_token"], "unf-1")

        assert response.status_code == 201
        machine = response.json()["machines"][0]
        assert machine["ami_id"] == "ami-0partial"
        assert machine["status"] == "creating"
        assert "unfrozen" in machine["error_message"]


class TestCaptureSingleFlight:
    def test_capture_rejected_while_builder_busy(self, env, engine, monkeypatch):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        with single_flight(builder_lock_key(builder_id)):
            response = save_template(env["room_id"], env["creator_token"], "busy-1")

        assert response.status_code == 409
        assert fake.created == []


class TestBuilderSelection:
    def test_stopped_builder_is_not_selected(self, env, engine, monkeypatch):
        add_builder(
            engine,
            env["room_id"],
            env["creator_id"],
            status="stopped",
            instance_id="i-0stopped",
        )
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        response = save_template(env["room_id"], env["creator_token"], "sel-stop")

        assert response.status_code == 409
        assert fake.created == []

    def test_newest_running_builder_is_selected(self, env, engine, db_session, monkeypatch):
        # Production uses a partial unique index (Postgres only); SQLite enforces
        # a full unique index, so drop it to exercise multi-row ordering.
        db_session.execute(text("DROP INDEX ix_one_active_builder_per_creator"))
        for status, instance_id, created_at in (
            ("stopped", "i-0stopped", datetime(2020, 1, 1)),
            ("running", "i-0running", datetime(2026, 1, 1)),
        ):
            db_session.add(
                BuilderEnvironment(
                    room_id=env["room_id"],
                    creator_id=env["creator_id"],
                    status=status,
                    instance_id=instance_id,
                    private_ip="10.0.1.174",
                    security_group_id="sg-0builder",
                    terraform_state_path="/tmp/secforge-builders/builder",
                    expires_at=datetime(2099, 1, 1),
                    created_at=created_at,
                )
            )
        db_session.commit()
        fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", fake)

        response = save_template(env["room_id"], env["creator_token"], "sel-new")

        assert response.status_code == 201
        assert fake.created[0]["instance_id"] == "i-0running"


class FakeRecoveryAMIManager:
    """Fake AMIManager for reconciliation tests; never touches AWS."""

    def __init__(self, existing_image=None, find_error=None,
                 create_result="ami-recovered", create_error=None,
                 image_state="available", state_error=None, find_delay=0.0):
        self.existing_image = existing_image
        self.find_error = find_error
        self.create_result = create_result
        self.create_error = create_error
        self.image_state = image_state
        self.state_error = state_error
        self.find_delay = find_delay
        self.find_calls = []
        self.create_calls = []
        self.state_calls = []

    def find_image_id_by_tags(self, tags):
        if self.find_delay:
            time.sleep(self.find_delay)
        self.find_calls.append(dict(tags))
        if self.find_error is not None:
            raise self.find_error
        return self.existing_image

    def create_image(
        self, instance_id, name, tags=None, no_reboot=True, client_token=None
    ):
        self.create_calls.append(
            {
                "instance_id": instance_id,
                "name": name,
                "tags": dict(tags or {}),
                "client_token": client_token,
            }
        )
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def get_image_state(self, image_id):
        self.state_calls.append(image_id)
        if self.state_error is not None:
            raise self.state_error
        return self.image_state


class TestStaleCaptureReconciliation:
    def _make_machine(
        self, db, env, builder_id, status="unknown", ami_id=None, age_minutes=60
    ):
        template = RoomTemplate(
            room_id=env["room_id"], version=1, created_by=env["creator_id"]
        )
        db.add(template)
        db.flush()
        machine = MachineTemplate(
            room_template_id=template.id,
            role="primary",
            source_builder_environment_id=builder_id,
            status=status,
            ami_id=ami_id,
            updated_at=datetime.utcnow() - timedelta(minutes=age_minutes),
        )
        db.add(machine)
        db.commit()
        return machine

    def _worker(self, manager):
        from app.reconciliation import ReconciliationWorker

        worker = ReconciliationWorker()
        # Inject the fake so reconciliation never constructs a real AWS client.
        worker._make_ami_manager = lambda: manager
        return worker

    @pytest.mark.parametrize("status", ["creating", "unknown"])
    def test_stale_machine_waits_for_tag_visibility(
        self, env, engine, db_session, status
    ):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        machine = self._make_machine(db_session, env, builder_id, status=status)
        manager = FakeRecoveryAMIManager()

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.ami_id is None
        assert machine.status == status
        assert manager.create_calls == []
        manager.existing_image = "ami-visible-later"
        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))
        db_session.refresh(machine)
        assert machine.ami_id == "ami-visible-later"
        assert machine.status == "available"
        assert manager.create_calls == []

    def test_interrupted_capture_with_null_ami_id_is_recovered(
        self, env, engine, db_session
    ):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        machine = self._make_machine(
            db_session, env, builder_id, status="creating", ami_id=None
        )
        manager = FakeRecoveryAMIManager(existing_image="ami-after-crash")

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.ami_id == "ami-after-crash"
        assert machine.status == "available"

    def test_existing_tagged_image_recovered_when_builder_gone(
        self, env, engine, db_session
    ):
        machine = self._make_machine(
            db_session, env, builder_id=None, status="unknown"
        )
        manager = FakeRecoveryAMIManager(existing_image="ami-existing")

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.ami_id == "ami-existing"
        assert machine.status == "available"
        assert manager.create_calls == []

    def test_recovered_ami_pending_keeps_creating(self, env, engine, db_session):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        machine = self._make_machine(db_session, env, builder_id, status="unknown")
        manager = FakeRecoveryAMIManager(
            existing_image="ami-existing", image_state="pending"
        )

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.ami_id == "ami-existing"
        assert machine.status == "creating"

    def test_transient_state_query_after_recovery_leaves_recoverable(
        self, env, engine, db_session
    ):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        machine = self._make_machine(db_session, env, builder_id, status="unknown")
        manager = FakeRecoveryAMIManager(
            existing_image="ami-existing", state_error=AMIError("throttled")
        )

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.ami_id == "ami-existing"
        assert machine.status == "creating"

    def test_no_image_and_no_builder_keeps_unknown(self, env, engine, db_session):
        machine = self._make_machine(
            db_session, env, builder_id=None, status="unknown"
        )
        manager = FakeRecoveryAMIManager(existing_image=None)

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.status == "unknown"
        assert machine.ami_id is None
        assert manager.create_calls == []

    def test_transient_search_error_leaves_row_recoverable(
        self, env, engine, db_session
    ):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        machine = self._make_machine(db_session, env, builder_id, status="unknown")
        manager = FakeRecoveryAMIManager(find_error=AMIError("throttled"))

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.status == "unknown"
        assert machine.ami_id is None

    def test_tag_lookup_is_safe_while_builder_guard_held(self, env, engine, db_session):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        machine = self._make_machine(db_session, env, builder_id, status="unknown")
        manager = FakeRecoveryAMIManager(existing_image="ami-existing")

        with single_flight(builder_lock_key(builder_id)):
            asyncio.run(
                self._worker(manager)._handle_stale_creating_machines(db_session)
            )

        db_session.refresh(machine)
        assert machine.ami_id == "ami-existing"
        assert machine.status == "available"
        assert manager.create_calls == []

    def test_repeated_recovery_never_creates_after_inconclusive_lookup(
        self, env, engine, db_session
    ):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        machine = self._make_machine(db_session, env, builder_id, status="unknown")
        manager = FakeRecoveryAMIManager(create_result="ami-once")
        worker = self._worker(manager)

        with single_flight(builder_lock_key(builder_id)):
            asyncio.run(worker._handle_stale_creating_machines(db_session))
        assert manager.create_calls == []

        asyncio.run(worker._handle_stale_creating_machines(db_session))
        db_session.refresh(machine)
        assert machine.ami_id is None
        assert machine.status == "unknown"
        asyncio.run(worker._handle_stale_creating_machines(db_session))
        assert manager.create_calls == []

    def test_recovery_does_not_block_event_loop(self, env, engine, db_session):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        self._make_machine(db_session, env, builder_id, status="unknown")
        manager = FakeRecoveryAMIManager(existing_image="ami-existing", find_delay=0.3)
        worker = self._worker(manager)

        async def run():
            ticks = 0

            async def heartbeat():
                nonlocal ticks
                for _ in range(6):
                    await asyncio.sleep(0.05)
                    ticks += 1

            await asyncio.gather(
                worker._handle_stale_creating_machines(db_session),
                heartbeat(),
            )
            return ticks

        assert asyncio.run(run()) >= 3

    def test_known_ami_finalized_available(self, env, engine, db_session):
        machine = self._make_machine(
            db_session, env, builder_id=None, status="creating", ami_id="ami-known"
        )
        manager = FakeRecoveryAMIManager(image_state="available")

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.status == "available"
        assert manager.state_calls == ["ami-known"]

    def test_known_ami_finalized_failed(self, env, engine, db_session):
        machine = self._make_machine(
            db_session, env, builder_id=None, status="creating", ami_id="ami-known"
        )
        manager = FakeRecoveryAMIManager(image_state="failed")

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.status == "failed"

    def test_transient_state_error_does_not_fail_known_ami(
        self, env, engine, db_session
    ):
        machine = self._make_machine(
            db_session, env, builder_id=None, status="creating", ami_id="ami-known"
        )
        manager = FakeRecoveryAMIManager(state_error=AMIError("throttled"))

        asyncio.run(self._worker(manager)._handle_stale_creating_machines(db_session))

        db_session.refresh(machine)
        assert machine.status == "creating"
        assert machine.ami_id == "ami-known"

    def test_recovery_uses_exact_persisted_capture_tags(
        self, env, engine, db_session, monkeypatch
    ):
        builder_id = add_builder(engine, env["room_id"], env["creator_id"])
        capture_fake = FakeAMIManager()
        monkeypatch.setattr(templates_module, "ami_manager", capture_fake)
        response = save_template(env["room_id"], env["creator_token"], "consistency")
        template_id = response.json()["id"]

        with Session(engine) as session:
            machine = session.scalars(
                select(MachineTemplate).where(
                    MachineTemplate.room_template_id == template_id
                )
            ).one()
            machine.status = "unknown"
            machine.ami_id = None
            machine.updated_at = datetime.utcnow() - timedelta(minutes=60)
            session.commit()
            machine_id = machine.id

        recovery_fake = FakeRecoveryAMIManager(existing_image="ami-recovered")
        asyncio.run(
            self._worker(recovery_fake)._handle_stale_creating_machines(db_session)
        )

        with Session(engine) as session:
            machine = session.get(MachineTemplate, machine_id)
            assert machine.ami_id == "ami-recovered"

        assert recovery_fake.find_calls[0] == capture_fake.created[0]["tags"]
        assert recovery_fake.create_calls == []
        assert recovery_fake.find_calls[0] == capture_image_tags(
            room_id=env["room_id"],
            template_id=template_id,
            version=1,
            builder_id=builder_id,
            capture_id=recovery_fake.find_calls[0]["CaptureRequestId"],
        )


class TestAmiResolution:
    def _make_template(self, db, room_id, creator_id, version, ami_id, status="available"):
        template = RoomTemplate(room_id=room_id, version=version, created_by=creator_id)
        db.add(template)
        db.flush()
        machine = MachineTemplate(
            room_template_id=template.id,
            role="primary",
            ami_id=ami_id,
            status=status,
        )
        db.add(machine)
        db.commit()
        return template

    def test_latest_available_used_when_draft(self, env, db_session):
        self._make_template(db_session, env["room_id"], env["creator_id"], 1, "ami-v1")
        self._make_template(db_session, env["room_id"], env["creator_id"], 2, "ami-v2")
        assert resolve_lab_ami(db_session, env["room_id"]) == "ami-v2"

    def test_published_pin_wins(self, env, db_session):
        self._make_template(db_session, env["room_id"], env["creator_id"], 1, "ami-v1")
        newest = self._make_template(db_session, env["room_id"], env["creator_id"], 2, "ami-v2")
        newest.is_published = True
        room = db_session.get(Room, env["room_id"])
        room.status = "published"
        db_session.commit()

        assert resolve_lab_ami(db_session, env["room_id"]) == "ami-v2"

    def test_published_pin_overrides_newer_version(self, env, db_session):
        pinned = self._make_template(db_session, env["room_id"], env["creator_id"], 1, "ami-v1")
        self._make_template(db_session, env["room_id"], env["creator_id"], 2, "ami-v2")
        pinned.is_published = True
        room = db_session.get(Room, env["room_id"])
        room.status = "published"
        db_session.commit()

        assert resolve_lab_ami(db_session, env["room_id"]) == "ami-v1"

    def test_no_template_returns_none(self, env, db_session):
        assert resolve_lab_ami(db_session, env["room_id"]) is None

    def test_only_available_used(self, env, db_session):
        self._make_template(
            db_session, env["room_id"], env["creator_id"], 1, "ami-v1", status="failed"
        )
        assert resolve_lab_ami(db_session, env["room_id"]) is None


class TestLabTfvarsAmi:
    def _lab(self):
        return LabInstance(
            id=7,
            room_id=1,
            learner_id=2,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=2),
        )

    def test_custom_ami_override(self):
        tfvars = LabManager()._generate_lab_tfvars(self._lab(), "ami-0template")
        assert 'ami_id           = "ami-0template"' in tfvars

    def test_missing_image_is_rejected(self):
        with pytest.raises(TemplateResolutionError):
            LabManager()._generate_lab_tfvars(self._lab())


class TestLearnerIdentityUserData:
    """P0: template-based learner labs regenerate machine-id and SSH host keys."""

    def _lab(self):
        return LabInstance(
            id=11,
            room_id=1,
            learner_id=2,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=2),
        )

    def test_script_regenerates_identity(self):
        script = LAB_FIRST_BOOT_USER_DATA
        assert "systemd-machine-id-setup" in script
        assert "/etc/machine-id" in script
        assert "/var/lib/dbus/machine-id" in script
        assert "rm -f /etc/ssh/ssh_host_*_key" in script
        assert "ssh-keygen -A" in script

    def test_script_is_idempotent_via_marker(self):
        script = LAB_FIRST_BOOT_USER_DATA
        assert "/var/lib/secforge/identity-initialized" in script
        # Exits before doing any work when the marker exists.
        assert script.index("exit 0") < script.index("systemd-machine-id-setup")

    def test_template_ami_supplies_user_data_in_tfvars(self):
        tfvars = LabManager()._generate_lab_tfvars(
            self._lab(), "ami-0template", LAB_FIRST_BOOT_USER_DATA
        )
        assert 'ami_id           = "ami-0template"' in tfvars
        assert "user_data        =" in tfvars
        assert "systemd-machine-id-setup" in tfvars
        assert "ssh-keygen -A" in tfvars

    def test_explicit_destroy_image_leaves_user_data_empty(self):
        tfvars = LabManager()._generate_lab_tfvars(self._lab(), "ami-offline-destroy")
        assert 'user_data        = ""' in tfvars
        assert "systemd-machine-id-setup" not in tfvars

    def test_generate_lab_user_data_returns_script(self):
        assert LabManager()._generate_lab_user_data() == LAB_FIRST_BOOT_USER_DATA


class TestTerraformUserDataWiring:
    """Terraform-level checks: user_data is parameterized for labs only."""

    @property
    def terraform_root(self) -> Path:
        return Path(__file__).resolve().parents[1] / "infrastructure" / "terraform"

    def test_lab_module_uses_parameterized_user_data(self):
        instance_tf = (
            self.terraform_root / "lab" / "modules" / "lab_env" / "instance.tf"
        ).read_text()
        assert "var.user_data" in instance_tf
        assert 'file("${path.module}/bootstrap.sh")' in instance_tf

    def test_lab_module_declares_user_data_variable(self):
        variables_tf = (
            self.terraform_root / "lab" / "modules" / "lab_env" / "variables.tf"
        ).read_text()
        assert 'variable "user_data"' in variables_tf

    def test_lab_root_passes_user_data(self):
        main_tf = (self.terraform_root / "lab" / "main.tf").read_text()
        variables_tf = (self.terraform_root / "lab" / "variables.tf").read_text()
        assert "user_data       = var.user_data" in main_tf
        assert 'variable "user_data"' in variables_tf

    def test_builder_terraform_unchanged(self):
        instance_tf = (
            self.terraform_root
            / "builder"
            / "modules"
            / "builder_env"
            / "instance.tf"
        ).read_text()
        assert "var.user_data" not in instance_tf
        assert 'user_data                   = file("${path.module}/bootstrap.sh")' in instance_tf

    def test_builder_root_not_wired_to_user_data(self):
        main_tf = (self.terraform_root / "builder" / "main.tf").read_text()
        variables_tf = (self.terraform_root / "builder" / "variables.tf").read_text()
        assert "user_data" not in main_tf
        assert 'variable "user_data"' not in variables_tf
