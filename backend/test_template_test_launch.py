#!/usr/bin/env python3
"""Authorized test launch from an explicitly selected saved template version."""

from datetime import datetime, timedelta
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import labs as labs_module
from app.config import settings
from app.database import Base, get_db
from app.lab_manager import LabManager, TemplateResolutionError
from app.labs import provision_lab_background as run_provision_background
from app.main import app
from app.models import LabInstance, MachineTemplate, Room, RoomTemplate, User
from app.security import create_access_token
from app.templates import resolve_lab_ami

client = TestClient(app)


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def force_dry_run(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "terraform_dry_run", True)
    monkeypatch.setattr(
        settings, "lab_terraform_workspace_root", str(tmp_path / "labs")
    )
    monkeypatch.setattr(
        settings, "builder_terraform_workspace_root", str(tmp_path / "builders")
    )


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
def db(engine):
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


@pytest.fixture
def env(engine):
    suffix = uuid4().hex[:8]
    with Session(engine) as session:
        creator = User(
            username=f"tl_creator_{suffix}",
            email=f"tl_creator_{suffix}@example.com",
            password_hash="x",
            role="creator",
        )
        other = User(
            username=f"tl_other_{suffix}",
            email=f"tl_other_{suffix}@example.com",
            password_hash="x",
            role="creator",
        )
        learner = User(
            username=f"tl_learner_{suffix}",
            email=f"tl_learner_{suffix}@example.com",
            password_hash="x",
            role="learner",
        )
        admin = User(
            username=f"tl_admin_{suffix}",
            email=f"tl_admin_{suffix}@example.com",
            password_hash="x",
            role="admin",
        )
        session.add_all([creator, other, learner, admin])
        session.flush()

        room = Room(
            creator_id=creator.id,
            name=f"Test Launch Room {suffix}",
            category="web",
            difficulty="easy",
            status="published",
        )
        session.add(room)
        session.commit()

        return {
            "creator_id": creator.id,
            "other_id": other.id,
            "learner_id": learner.id,
            "admin_id": admin.id,
            "room_id": room.id,
            "creator_token": create_access_token(creator.id, creator.role),
            "other_token": create_access_token(other.id, other.role),
            "learner_token": create_access_token(learner.id, learner.role),
            "admin_token": create_access_token(admin.id, admin.role),
        }


def seed_template(
    engine,
    room_id,
    creator_id,
    version,
    ami_id="ami-0test",
    status="available",
    published=False,
):
    with Session(engine) as session:
        template = RoomTemplate(
            room_id=room_id,
            version=version,
            created_by=creator_id,
            is_published=published,
        )
        session.add(template)
        session.flush()
        session.add(
            MachineTemplate(
                room_template_id=template.id,
                role="primary",
                ami_id=ami_id,
                status=status,
            )
        )
        session.commit()
        return template.id


def post_test_launch(room_id, template_id, token, ttl_hours=4):
    return client.post(
        f"/api/rooms/{room_id}/templates/{template_id}/lab",
        json={"ttl_hours": ttl_hours},
        headers=auth_header(token),
    )


@pytest.fixture
def provision_recorder(monkeypatch):
    calls = []

    def _record(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(labs_module, "provision_lab_background", _record)
    return calls


class TestTestLaunchAuthz:
    def test_learner_forbidden(self, env):
        response = post_test_launch(env["room_id"], 1, env["learner_token"])
        assert response.status_code == 403

    def test_non_owner_creator_not_found(self, env, engine):
        template_id = seed_template(engine, env["room_id"], env["creator_id"], 1)
        response = post_test_launch(env["room_id"], template_id, env["other_token"])
        assert response.status_code == 404

    def test_owner_allowed(self, env, engine, provision_recorder):
        template_id = seed_template(
            engine, env["room_id"], env["creator_id"], 1, ami_id="ami-v2"
        )

        response = post_test_launch(env["room_id"], template_id, env["creator_token"])
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["room_id"] == env["room_id"]

        assert provision_recorder == [(body["id"], template_id, "ami-v2")]

    def test_admin_cannot_bypass_creator_ownership(self, env, engine, provision_recorder):
        template_id = seed_template(engine, env["room_id"], env["creator_id"], 1)

        response = post_test_launch(env["room_id"], template_id, env["admin_token"])
        assert response.status_code == 404
        assert provision_recorder == []


class TestTestLaunchValidation:
    def test_unknown_template_not_found(self, env, engine, provision_recorder):
        response = post_test_launch(env["room_id"], 9999, env["creator_token"])
        assert response.status_code == 404
        assert provision_recorder == []


    def test_template_from_other_room_not_found(self, env, engine, provision_recorder):
        with Session(engine) as session:
            other_room = Room(
                creator_id=env["creator_id"],
                name="Other Room",
                category="web",
                difficulty="easy",
                status="published",
            )
            session.add(other_room)
            session.commit()
            other_room_id = other_room.id

        foreign_template_id = seed_template(
            engine, other_room_id, env["creator_id"], 1
        )

        response = post_test_launch(
            env["room_id"], foreign_template_id, env["creator_token"]
        )
        assert response.status_code == 404
        assert provision_recorder == []

    def test_unavailable_ami_conflict(self, env, engine, provision_recorder, db):
        template_id = seed_template(
            engine,
            env["room_id"],
            env["creator_id"],
            1,
            ami_id="ami-pending",
            status="creating",
        )

        response = post_test_launch(env["room_id"], template_id, env["creator_token"])
        assert response.status_code == 409
        assert provision_recorder == []
        assert db.query(LabInstance).count() == 0

    def test_no_ami_conflict(self, env, engine, provision_recorder, db):
        template_id = seed_template(
            engine,
            env["room_id"],
            env["creator_id"],
            1,
            ami_id=None,
            status="available",
        )

        response = post_test_launch(env["room_id"], template_id, env["creator_token"])
        assert response.status_code == 409
        assert db.query(LabInstance).count() == 0

    def test_ttl_out_of_range_rejected(self, env, engine, provision_recorder):
        template_id = seed_template(engine, env["room_id"], env["creator_id"], 1)
        response = post_test_launch(
            env["room_id"], template_id, env["creator_token"], ttl_hours=99
        )
        assert response.status_code == 400
        assert provision_recorder == []


def seed_lifecycle_lab(engine, env, user="creator", status="running"):
    with Session(engine) as db:
        lab = LabInstance(
            room_id=env["room_id"], learner_id=env[f"{user}_id"], status=status,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.add(lab)
        db.commit()
        return lab.id


def creator_lab_request(env, token="creator", stop=False):
    path = f"/api/rooms/{env['room_id']}/test-lab" + ("/stop" if stop else "")
    method = client.post if stop else client.get
    return method(path, headers=auth_header(env[f"{token}_token"]))


class TestCreatorLifecycle:
    @pytest.mark.parametrize("stop", [False, True])
    def test_lifecycle_requires_authentication(self, env, engine, monkeypatch, stop):
        lab_id = seed_lifecycle_lab(engine, env)
        destroy = MagicMock()
        monkeypatch.setattr(labs_module, "destroy_lab_background", destroy)
        path = f"/api/rooms/{env['room_id']}/test-lab" + ("/stop" if stop else "")
        response = client.post(path) if stop else client.get(path)
        assert response.status_code == 401
        destroy.assert_not_called()
        with Session(engine) as db:
            assert db.get(LabInstance, lab_id).status == "running"

    def test_draft_room_test_launch_is_manageable_without_a_builder(self, env, engine, provision_recorder, monkeypatch):
        with Session(engine) as db:
            db.get(Room, env["room_id"]).status = "draft"
            db.commit()
        template = seed_template(engine, env["room_id"], env["creator_id"], 1)
        response = post_test_launch(env["room_id"], template, env["creator_token"])
        assert response.status_code == 201
        lab_id = response.json()["id"]
        assert creator_lab_request(env).json()["id"] == lab_id
        destroy = MagicMock()
        monkeypatch.setattr(labs_module, "destroy_lab_background", destroy)
        assert creator_lab_request(env, stop=True).status_code == 200
        destroy.assert_called_once_with(lab_id)
        assert provision_recorder == [(lab_id, template, "ami-0test")]

    def test_commit_failure_rolls_back_pending_lab_and_schedules_nothing(self, env, engine, monkeypatch, provision_recorder):
        template = seed_template(engine, env["room_id"], env["creator_id"], 1)

        def unavailable(self):
            raise SQLAlchemyError("internal-commit-marker")

        monkeypatch.setattr(Session, "commit", unavailable)
        response = post_test_launch(env["room_id"], template, env["creator_token"])
        assert response.status_code == 503
        assert "internal-commit-marker" not in response.text
        assert provision_recorder == []
        with Session(engine) as db:
            assert db.query(LabInstance).count() == 0

    def test_selected_image_change_before_background_never_substitutes(self, env, engine, monkeypatch, provision_recorder):
        template = seed_template(engine, env["room_id"], env["creator_id"], 1, ami_id="ami-selected")
        response = post_test_launch(env["room_id"], template, env["creator_token"])
        assert response.status_code == 201
        with Session(engine) as db:
            db.execute(update(MachineTemplate).where(MachineTemplate.room_template_id == template).values(ami_id="ami-substitute"))
            db.commit()
        monkeypatch.setattr(labs_module, "SessionLocal", sessionmaker(bind=engine))
        terraform = MagicMock()
        monkeypatch.setattr(LabManager, "_run_terraform", terraform)
        run_provision_background(*provision_recorder[0])
        terraform.assert_not_called()
        with Session(engine) as db:
            assert db.get(LabInstance, response.json()["id"]).status == "failed"

    @pytest.mark.parametrize("state", ["pending", "provisioning", "running", "stopping", "stopped", "failed"])
    def test_owner_can_inspect_all_states_without_builder(self, env, engine, state):
        lab_id = seed_lifecycle_lab(engine, env, status=state)
        response = creator_lab_request(env)
        assert response.status_code == 200
        assert response.json()["id"] == lab_id
        assert response.json()["status"] == state
        assert "learner_id" not in response.json()
        assert "terraform_state_path" not in response.json()

    @pytest.mark.parametrize("stop", [False, True])
    @pytest.mark.parametrize("token,expected", [("other", 404), ("learner", 403), ("admin", 404)])
    def test_other_users_cannot_manage_creator_lab(self, env, engine, monkeypatch, stop, token, expected):
        lab_id = seed_lifecycle_lab(engine, env)
        destroy = MagicMock()
        monkeypatch.setattr(labs_module, "destroy_lab_background", destroy)
        assert creator_lab_request(env, token, stop).status_code == expected
        destroy.assert_not_called()
        with Session(engine) as db:
            assert db.get(LabInstance, lab_id).status == "running"

    @pytest.mark.parametrize("stop", [False, True])
    def test_creator_cannot_select_learner_lab_even_in_own_room(self, env, engine, monkeypatch, stop):
        learner_lab_id = seed_lifecycle_lab(engine, env, user="learner")
        destroy = MagicMock()
        monkeypatch.setattr(labs_module, "destroy_lab_background", destroy)
        assert creator_lab_request(env, stop=stop).status_code == 404
        destroy.assert_not_called()
        with Session(engine) as db:
            assert db.get(LabInstance, learner_lab_id).status == "running"

    @pytest.mark.parametrize("state", ["pending", "provisioning", "running"])
    def test_stop_claims_only_creators_lab_and_repeated_stop_defers(self, env, engine, monkeypatch, state):
        creator_lab_id = seed_lifecycle_lab(engine, env, status=state)
        learner_lab_id = seed_lifecycle_lab(engine, env, user="learner")
        calls = []
        monkeypatch.setattr(labs_module, "destroy_lab_background", lambda lab_id: calls.append(lab_id))
        response = creator_lab_request(env, stop=True)
        assert response.status_code == 200 and response.json()["status"] == "stopping"
        assert response.json()["id"] == creator_lab_id
        assert calls == [creator_lab_id]
        assert creator_lab_request(env, stop=True).status_code == 200
        assert calls == [creator_lab_id]
        with Session(engine) as db:
            assert db.get(LabInstance, learner_lab_id).status == "running"
            assert db.get(LabInstance, creator_lab_id).status == "stopping"

    @pytest.mark.parametrize("state", ["stopping", "stopped", "failed"])
    def test_terminal_or_already_stopping_lab_does_not_schedule_destroy(self, env, engine, monkeypatch, state):
        lab_id = seed_lifecycle_lab(engine, env, status=state)
        destroy = MagicMock()
        monkeypatch.setattr(labs_module, "destroy_lab_background", destroy)
        assert creator_lab_request(env, stop=True).status_code == (409 if state == "failed" else 200)
        destroy.assert_not_called()
        with Session(engine) as db:
            assert db.get(LabInstance, lab_id).status == state

    def test_admin_inspects_only_their_own_test_launch(self, env, engine):
        lab_id = seed_lifecycle_lab(engine, env, user="admin")
        assert creator_lab_request(env, "admin").status_code == 404
        with Session(engine) as db:assert db.get(LabInstance,lab_id).status=="running"
        assert creator_lab_request(env).status_code == 404

    def test_learner_routes_stay_role_scoped_and_return_the_learners_row(self, env, engine, monkeypatch):
        creator_lab = seed_lifecycle_lab(engine, env)
        learner_lab = seed_lifecycle_lab(engine, env, user="learner")
        calls = []
        monkeypatch.setattr(labs_module, "destroy_lab_background", lambda lab_id: calls.append(lab_id))
        path = f"/api/rooms/{env['room_id']}/lab"
        assert client.get(path, headers=auth_header(env["creator_token"])).status_code == 404
        assert client.post(path + "/stop", headers=auth_header(env["creator_token"])).status_code == 404
        response = client.get(path, headers=auth_header(env["learner_token"]))
        assert response.status_code == 200 and response.json()["id"] == learner_lab
        assert client.post(path + "/stop", headers=auth_header(env["learner_token"])).status_code == 200
        assert calls == [learner_lab]
        with Session(engine) as db:
            assert db.get(LabInstance, creator_lab).status == "running"

    def test_actual_background_destroy_uses_shared_manager_with_offline_terraform(self, env, engine, monkeypatch):
        lab_id = seed_lifecycle_lab(engine, env)
        monkeypatch.setattr(labs_module, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr("app.lab_manager.sync_vpn_authz", lambda db: None)
        calls = []

        def prepare(self, lab, *args):
            workspace = Path(lab.terraform_state_path)
            workspace.mkdir(parents=True, exist_ok=True)
            return str(workspace)

        def terraform(self, cmd, **kwargs):
            calls.append(cmd[0])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(LabManager, "_prepare_lab_workspace", prepare)
        monkeypatch.setattr(LabManager, "_run_terraform", terraform)
        assert creator_lab_request(env, stop=True).status_code == 200
        assert creator_lab_request(env).json()["status"] == "stopped"
        assert calls == ["init", "destroy"]
        with Session(engine) as db:
            row = db.get(LabInstance, lab_id)
            assert row.stopped_at is not None and row.instance_id is None

    def test_selected_version_launch_status_and_stop_through_real_background_paths(self, env, engine, monkeypatch):
        pinned = seed_template(engine, env["room_id"], env["creator_id"], 1, ami_id="ami-pinned", published=True)
        selected = seed_template(engine, env["room_id"], env["creator_id"], 2, ami_id="ami-selected")
        monkeypatch.setattr(labs_module, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr("app.lab_manager.authorize_lab_access", lambda db, lab: None)
        monkeypatch.setattr("app.lab_manager.sync_vpn_authz", lambda db: None)
        calls, images = [], []

        def prepare(self, lab, ami_id, *args):
            images.append(ami_id)
            workspace = Path(lab.terraform_state_path)
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "backend.tf").write_text(self._generate_lab_backend_config(lab))
            return str(workspace)

        def terraform(self, cmd, **kwargs):
            calls.append(cmd[0])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(LabManager, "_prepare_lab_workspace", prepare)
        monkeypatch.setattr(LabManager, "_run_terraform", terraform)
        monkeypatch.setattr(LabManager, "_parse_terraform_outputs", lambda *args: {
            "instance_id": "i-offline", "private_ip": "10.0.0.20", "security_group_id": "sg-offline",
        })
        response = post_test_launch(env["room_id"], selected, env["creator_token"])
        assert response.status_code == 201
        lab_id = response.json()["id"]
        status_response = creator_lab_request(env)
        assert status_response.status_code == 200
        assert status_response.json()["id"] == lab_id
        assert status_response.json()["status"] == "running"
        assert images == ["ami-selected"]
        assert calls == ["init", "apply"]
        assert creator_lab_request(env, stop=True).status_code == 200
        assert creator_lab_request(env).json()["status"] == "stopped"
        assert calls == ["init", "apply", "init", "destroy"]
        assert images == ["ami-selected"]
        assert creator_lab_request(env, stop=True).status_code == 200
        assert calls == ["init", "apply", "init", "destroy"]
        with Session(engine) as db:
            assert db.get(RoomTemplate, pinned).is_published
            assert not db.get(RoomTemplate, selected).is_published

    @pytest.mark.parametrize("operation", ["status", "stop", "launch"])
    def test_database_failure_returns_safe_503_without_task(self, env, engine, monkeypatch, provision_recorder, operation):
        template_id = seed_template(engine, env["room_id"], env["creator_id"], 1)
        destroy = MagicMock()
        monkeypatch.setattr(labs_module, "destroy_lab_background", destroy)

        def unavailable(*args):
            raise SQLAlchemyError("internal-diagnostic-marker")

        monkeypatch.setattr(labs_module, "_test_room_or_404", unavailable)
        response = post_test_launch(env["room_id"], template_id, env["creator_token"]) if operation == "launch" else creator_lab_request(env, stop=operation == "stop")
        assert response.status_code == 503
        assert "internal-diagnostic-marker" not in response.text
        assert provision_recorder == []
        destroy.assert_not_called()
        with Session(engine) as db:
            assert db.query(LabInstance).count() == 0

    def test_duplicate_test_launch_does_not_change_pin_or_schedule_again(self, env, engine, provision_recorder):
        pinned = seed_template(engine, env["room_id"], env["creator_id"], 1, ami_id="ami-pinned", published=True)
        selected = seed_template(engine, env["room_id"], env["creator_id"], 2, ami_id="ami-selected")
        response = post_test_launch(env["room_id"], selected, env["creator_token"])
        assert response.status_code == 201
        assert provision_recorder == [(response.json()["id"], selected, "ami-selected")]
        assert post_test_launch(env["room_id"], pinned, env["creator_token"]).status_code == 409
        assert len(provision_recorder) == 1
        with Session(engine) as db:
            assert db.query(LabInstance).count() == 1
            assert db.get(RoomTemplate, pinned).is_published
            assert not db.get(RoomTemplate, selected).is_published

    @pytest.mark.parametrize("image", ["", "   "])
    def test_blank_image_rejected_without_record_or_task(self, env, engine, provision_recorder, image):
        template = seed_template(engine, env["room_id"], env["creator_id"], 1, ami_id=image)
        assert post_test_launch(env["room_id"], template, env["creator_token"]).status_code == 409
        assert provision_recorder == []
        with Session(engine) as db:
            assert db.query(LabInstance).count() == 0


class TestDefaultBehaviorPreserved:
    def _pin_v1(self, engine, env, ami_id="ami-v1"):
        room = None
        with Session(engine) as session:
            room = session.get(Room, env["room_id"])
            room.status = "published"
            session.commit()

        return seed_template(
            engine,
            env["room_id"],
            env["creator_id"],
            1,
            ami_id=ami_id,
            published=True,
        )

    def test_published_pin_unchanged_after_test_launch(self, env, engine, db):
        pinned = self._pin_v1(engine, env)
        newer = seed_template(
            engine, env["room_id"], env["creator_id"], 2, ami_id="ami-v2"
        )

        assert resolve_lab_ami(db, env["room_id"]) == "ami-v1"

        response = post_test_launch(env["room_id"], newer, env["creator_token"])
        assert response.status_code == 201

        room = db.get(Room, env["room_id"])
        assert room.status == "published"
        pinned_row = db.get(RoomTemplate, pinned)
        assert pinned_row.is_published is True
        newer_row = db.get(RoomTemplate, newer)
        assert newer_row.is_published is False
        assert resolve_lab_ami(db, env["room_id"]) == "ami-v1"

    def test_ordinary_learner_launch_unchanged(self, env, engine, provision_recorder):
        self._pin_v1(engine, env)

        response = client.post(
            f"/api/rooms/{env['room_id']}/lab",
            json={"ttl_hours": 4},
            headers=auth_header(env["learner_token"]),
        )
        assert response.status_code == 201
        assert provision_recorder[0][1] is not None
        assert provision_recorder[0][2] == "ami-v1"


class TestExplicitResolution:
    def _pin(self, db, room_id, creator_id, version, ami_id):
        template = RoomTemplate(
            room_id=room_id,
            version=version,
            created_by=creator_id,
            is_published=True,
        )
        db.add(template)
        db.flush()
        db.add(
            MachineTemplate(
                room_template_id=template.id,
                role="primary",
                ami_id=ami_id,
                status="available",
            )
        )
        room = db.get(Room, room_id)
        room.status = "published"
        db.commit()
        return template.id

    def _template(self, db, room_id, creator_id, version, ami_id, status="available"):
        template = RoomTemplate(
            room_id=room_id, version=version, created_by=creator_id
        )
        db.add(template)
        db.flush()
        db.add(
            MachineTemplate(
                room_template_id=template.id,
                role="primary",
                ami_id=ami_id,
                status=status,
            )
        )
        db.commit()
        return template.id

    def test_explicit_wins_over_published_pin(self, env, db):
        self._pin(db, env["room_id"], env["creator_id"], 1, "ami-v1")
        explicit = self._template(
            db, env["room_id"], env["creator_id"], 2, "ami-v2"
        )

        assert resolve_lab_ami(db, env["room_id"]) == "ami-v1"
        assert resolve_lab_ami(db, env["room_id"], explicit) == "ami-v2"

    def test_explicit_from_other_room_returns_none(self, env, db):
        with Session(db.get_bind()) as session:
            other_room = Room(
                creator_id=env["creator_id"],
                name="Other",
                category="web",
                difficulty="easy",
                status="published",
            )
            session.add(other_room)
            session.commit()
            other_room_id = other_room.id

        foreign = self._template(
            db, other_room_id, env["creator_id"], 1, "ami-foreign"
        )
        assert resolve_lab_ami(db, env["room_id"], foreign) is None

    def test_explicit_unknown_returns_none(self, env, db):
        assert resolve_lab_ami(db, env["room_id"], 123456) is None

    def test_explicit_unavailable_never_falls_back(self, env, db):
        self._pin(db, env["room_id"], env["creator_id"], 1, "ami-v1")
        unavailable = self._template(
            db, env["room_id"], env["creator_id"], 2, "ami-bad", status="creating"
        )

        assert resolve_lab_ami(db, env["room_id"], unavailable) is None


class TestStartLabInstanceExplicit:
    def _lab(self, db, room_id, learner_id):
        lab = LabInstance(
            room_id=room_id,
            learner_id=learner_id,
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=2),
        )
        db.add(lab)
        db.commit()
        return lab

    def _template(self, db, room_id, creator_id, version, ami_id, status, published=False):
        template = RoomTemplate(
            room_id=room_id,
            version=version,
            created_by=creator_id,
            is_published=published,
        )
        db.add(template)
        db.flush()
        db.add(
            MachineTemplate(
                room_template_id=template.id,
                role="primary",
                ami_id=ami_id,
                status=status,
            )
        )
        db.commit()
        return template.id

    def test_explicit_available_template_is_used(self, env, db):
        self._template(
            db, env["room_id"], env["creator_id"], 1, "ami-pinned", "available", True
        )
        explicit = self._template(
            db, env["room_id"], env["creator_id"], 2, "ami-explicit", "available"
        )
        lab = self._lab(db, env["room_id"], env["learner_id"])

        LabManager().start_lab_instance(lab, db, room_template_id=explicit)
        db.refresh(lab)

        assert lab.status == "running"
        tfvars = (Path(lab.terraform_state_path) / "terraform.tfvars").read_text()
        assert 'ami_id           = "ami-explicit"' in tfvars
        assert "ami-pinned" not in tfvars

    def test_explicit_unavailable_fails_without_fallback(self, env, db):
        self._template(
            db, env["room_id"], env["creator_id"], 1, "ami-pinned", "available", True
        )
        unavailable = self._template(
            db, env["room_id"], env["creator_id"], 2, "ami-bad", "creating"
        )
        lab = self._lab(db, env["room_id"], env["learner_id"])

        with patch.object(
            LabManager,
            "_run_terraform",
            side_effect=AssertionError("terraform must not run without a resolved AMI"),
        ):
            with pytest.raises(TemplateResolutionError):
                LabManager().start_lab_instance(
                    lab, db, room_template_id=unavailable
                )

        db.refresh(lab)
        assert lab.status == "failed"
        assert lab.error_message == "Selected template version has no available AMI"
        assert lab.terraform_state_path is None
        assert not Path(LabManager().lab_workspace_path(lab)).exists()


class TestLearnerChallengeImages:
    def _launch(self, env):
        return client.post(
            f"/api/rooms/{env['room_id']}/lab",
            json={"ttl_hours": 1},
            headers=auth_header(env["learner_token"]),
        )

    @pytest.mark.parametrize("pinned", [True, False])
    def test_valid_selection_is_passed_to_background(self, env, engine, provision_recorder, pinned):
        older = seed_template(engine, env["room_id"], env["creator_id"], 1,
                              ami_id="ami-older", published=pinned)
        newer = seed_template(engine, env["room_id"], env["creator_id"], 2, ami_id="ami-newer")
        response = self._launch(env)
        assert response.status_code == 201
        assert provision_recorder == [(response.json()["id"],
                                       older if pinned else newer,
                                       "ami-older" if pinned else "ami-newer")]

    @pytest.mark.parametrize("status,image", [
        (None, None), ("creating", "ami-pending"), ("failed", "ami-failed"),
        ("available", None), ("available", ""), ("available", "   "),
    ])
    def test_unusable_image_rejected_before_record_or_task(
        self, env, engine, db, provision_recorder, status, image
    ):
        if status is not None:
            seed_template(engine, env["room_id"], env["creator_id"], 1,
                          ami_id=image, status=status)
        with patch.object(LabManager, "_run_terraform") as terraform:
            response = self._launch(env)
        assert response.status_code == 409
        assert provision_recorder == []
        assert db.scalars(select(LabInstance)).all() == []
        terraform.assert_not_called()

    @pytest.mark.parametrize("status,image", [
        ("creating", "ami-pending"), ("failed", "ami-failed"),
        ("available", None), ("available", ""), ("missing", None),
    ])
    def test_invalid_pin_never_uses_another_version(
        self, env, engine, db, provision_recorder, status, image
    ):
        pinned = seed_template(engine, env["room_id"], env["creator_id"], 1,
                               ami_id=image, status=status, published=True)
        if status == "missing":
            db.delete(db.scalar(select(MachineTemplate).where(
                MachineTemplate.room_template_id == pinned)))
            db.commit()
        seed_template(engine, env["room_id"], env["creator_id"], 2)
        assert self._launch(env).status_code == 409
        assert provision_recorder == []
        assert db.scalars(select(LabInstance)).all() == []

    def test_database_error_rejected_before_record_or_task(
        self, env, db, provision_recorder, monkeypatch
    ):
        def unavailable(*args):
            raise SQLAlchemyError("offline simulated database failure")
        monkeypatch.setattr(labs_module, "resolve_lab_machine", unavailable)
        response = self._launch(env)
        assert response.status_code == 503
        assert "database failure" not in response.text
        assert provision_recorder == []
        assert db.scalars(select(LabInstance)).all() == []

    @pytest.mark.parametrize("change", ["creating", "failed", "missing", "changed-image", "database-error"])
    def test_background_revalidates_without_provisioning_or_substitution(
        self, env, engine, db, provision_recorder, monkeypatch, change
    ):
        selected = seed_template(engine, env["room_id"], env["creator_id"], 1,
                                 ami_id="ami-selected")
        response = self._launch(env)
        assert response.status_code == 201
        lab_id, template_id, image_id = provision_recorder[0]
        assert template_id == selected
        seed_template(engine, env["room_id"], env["creator_id"], 2, ami_id="ami-substitute")
        machine = db.scalar(select(MachineTemplate).where(MachineTemplate.room_template_id == selected))
        if change == "missing":
            db.delete(machine)
            db.flush()
            db.delete(db.get(RoomTemplate, selected))
        elif change == "changed-image":
            machine.ami_id = "ami-replaced"
        elif change != "database-error":
            machine.status = change
        db.commit()
        if change == "database-error":
            def unavailable(*args):
                raise SQLAlchemyError("offline simulated database failure")
            monkeypatch.setattr("app.templates.resolve_lab_ami", unavailable)
        lab = db.get(LabInstance, lab_id)
        manager = LabManager()
        with patch.object(manager, "_run_terraform") as terraform:
            with pytest.raises(TemplateResolutionError):
                manager.start_lab_instance(lab, db, template_id, image_id)
        terraform.assert_not_called()
        db.refresh(lab)
        assert lab.status == "failed"
        assert lab.instance_id is None
        assert lab.terraform_state_path is None
        assert not Path(manager.lab_workspace_path(lab)).exists()

    def test_newer_capture_does_not_switch_background_selection(
        self, env, engine, db, provision_recorder
    ):
        selected = seed_template(engine, env["room_id"], env["creator_id"], 1,
                                 ami_id="ami-selected")
        response = self._launch(env)
        assert response.status_code == 201
        lab_id, template_id, image_id = provision_recorder[0]
        assert template_id == selected
        seed_template(engine, env["room_id"], env["creator_id"], 2, ami_id="ami-newer")
        lab = db.get(LabInstance, lab_id)
        LabManager().start_lab_instance(lab, db, template_id, image_id)
        db.refresh(lab)
        assert lab.status == "running"
        tfvars = (Path(lab.terraform_state_path) / "terraform.tfvars").read_text()
        assert 'ami_id           = "ami-selected"' in tfvars
        assert "ami-newer" not in tfvars

    def test_newer_pending_capture_is_skipped(self, env, engine, provision_recorder):
        selected = seed_template(engine, env["room_id"], env["creator_id"], 1)
        seed_template(engine, env["room_id"], env["creator_id"], 2, status="creating")
        response = self._launch(env)
        assert response.status_code == 201
        assert provision_recorder == [(response.json()["id"], selected, "ami-0test")]

    def test_selected_available_machine_survives_primary_machine_unavailability(
        self, env, engine, db, provision_recorder
    ):
        selected = seed_template(engine, env["room_id"], env["creator_id"], 1, status="failed")
        db.add(MachineTemplate(room_template_id=selected, role="primary",
                               ami_id="ami-selected", status="available"))
        db.commit()
        response = self._launch(env)
        assert response.status_code == 201
        lab_id, template_id, image_id = provision_recorder[0]
        lab = db.get(LabInstance, lab_id)
        LabManager().start_lab_instance(lab, db, template_id, image_id)
        db.refresh(lab)
        assert lab.status == "running"
        assert 'ami_id           = "ami-selected"' in (
            Path(lab.terraform_state_path) / "terraform.tfvars"
        ).read_text()

    @pytest.mark.parametrize("resolution", [None, "ami-selected"])
    def test_concurrent_stop_is_not_overwritten(
        self, env, engine, db, provision_recorder, monkeypatch, resolution
    ):
        seed_template(engine, env["room_id"], env["creator_id"], 1, ami_id="ami-selected")
        response = self._launch(env)
        lab_id, template_id, image_id = provision_recorder[0]
        lab = db.get(LabInstance, lab_id)
        def stop_during_resolution(*args):
            lab.status = "stopping"
            db.commit()
            return resolution
        monkeypatch.setattr("app.templates.resolve_lab_ami", stop_during_resolution)
        manager = LabManager()
        with patch.object(manager, "_run_terraform") as terraform:
            if resolution is None:
                with pytest.raises(TemplateResolutionError):
                    manager.start_lab_instance(lab, db, template_id, image_id)
            else:
                manager.start_lab_instance(lab, db, template_id, image_id)
        terraform.assert_not_called()
        db.refresh(lab)
        assert lab.status == "stopping"
        assert lab.terraform_state_path is None

    def test_resolution_database_errors_propagate(self):
        db = MagicMock()
        db.get.side_effect = SQLAlchemyError("offline simulated database failure")
        with pytest.raises(SQLAlchemyError):
            resolve_lab_ami(db, 1)

    def test_rejection_preserves_existing_terminal_lab(self, env, db, provision_recorder):
        old_lab = LabInstance(room_id=env["room_id"], learner_id=env["learner_id"],
                              status="stopped", expires_at=datetime.utcnow())
        db.add(old_lab)
        db.commit()
        assert self._launch(env).status_code == 409
        db.refresh(old_lab)
        assert old_lab.status == "stopped"
        assert db.scalars(select(LabInstance)).all() == [old_lab]
        assert provision_recorder == []

    def test_failed_validation_preserves_existing_workspace(self, env, db, tmp_path):
        workspace = tmp_path / "existing-workspace"
        workspace.mkdir()
        marker = workspace / "preserve.txt"
        marker.write_text("offline test marker")
        lab = LabInstance(room_id=env["room_id"], learner_id=env["learner_id"],
                          status="pending", expires_at=datetime.utcnow() + timedelta(hours=1),
                          terraform_state_path=str(workspace))
        db.add(lab)
        db.commit()
        manager = LabManager()
        manager.config["terraform_dry_run"] = False
        with patch.object(manager, "_run_terraform") as terraform:
            with pytest.raises(TemplateResolutionError):
                manager.start_lab_instance(lab, db)
        terraform.assert_not_called()
        db.refresh(lab)
        assert lab.status == "failed"
        assert marker.read_text() == "offline test marker"
