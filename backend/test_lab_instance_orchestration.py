#!/usr/bin/env python3
"""Tests for learner lab instance Terraform orchestration."""

import asyncio
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.lab_manager import LabManager, TerraformError
from app.labs import destroy_lab_background, stop_lab
from app.models import BuilderEnvironment, LabInstance, MachineTemplate, Room, RoomTemplate, User
from app.ops_lock import (
    OperationInProgress,
    builder_lock_key,
    lab_lock_key,
    single_flight,
)
from app.reconciliation import ReconciliationWorker
from app.vpn_authz import VpnAuthorizationError

ORIGINAL_RUN_TERRAFORM = LabManager._run_terraform


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
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def make_people(db, suffix="one"):
    creator = User(
        username=f"orch_creator_{suffix}",
        email=f"orch_creator_{suffix}@example.com",
        password_hash="hash",
        role="creator",
    )
    learner = User(
        username=f"orch_learner_{suffix}",
        email=f"orch_learner_{suffix}@example.com",
        password_hash="hash",
        role="learner",
    )
    db.add_all([creator, learner])
    db.flush()
    room = Room(
        name=f"Orchestration Room {suffix}",
        category="Web",
        difficulty="easy",
        creator_id=creator.id,
        status="published",
    )
    db.add(room)
    db.flush()
    template = RoomTemplate(room_id=room.id, version=1, created_by=creator.id)
    db.add(template)
    db.flush()
    db.add(MachineTemplate(room_template_id=template.id, status="available", ami_id="ami-offline-challenge"))
    db.flush()
    return creator, learner, room


def make_lab(db, suffix="one", status="pending", expires_at=None, **overrides):
    creator, learner, room = make_people(db, suffix)
    lab = LabInstance(
        room_id=room.id,
        learner_id=learner.id,
        status=status,
        expires_at=expires_at or datetime.utcnow() + timedelta(hours=2),
        **overrides,
    )
    db.add(lab)
    db.flush()
    db.commit()
    return lab


def failing_command(args, returncode=1, stderr="apply exploded"):
    def _failing(self, cmd, **kwargs):
        if cmd and cmd[0] == args:
            return subprocess.CompletedProcess(
                args=cmd, returncode=returncode, stdout="", stderr=stderr
            )
        return ORIGINAL_RUN_TERRAFORM(self, cmd, **kwargs)

    return _failing


def recording_terraform(calls):
    def _rec(self, cmd, **kwargs):
        calls.append(cmd)
        return ORIGINAL_RUN_TERRAFORM(self, cmd, **kwargs)

    return _rec


def stopping_grace() -> timedelta:
    return timedelta(
        seconds=settings.terraform_timeout_seconds
        + settings.builder_reconciliation_interval_seconds
    )


def make_builder(db, tmp_path, suffix="builder", status="running", **overrides):
    creator, _learner, room = make_people(db, suffix)
    builder = BuilderEnvironment(
        room_id=room.id,
        creator_id=creator.id,
        status=status,
        instance_id="i-builder",
        private_ip="10.0.1.50",
        security_group_id="sg-builder",
        terraform_state_path="",
        expires_at=datetime.utcnow() + timedelta(hours=2),
        **overrides,
    )
    db.add(builder)
    db.flush()
    builder.terraform_state_path = str(tmp_path / "builders" / f"builder-{builder.id}")
    db.commit()
    return builder


@pytest.fixture
def db_file(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    yield maker
    engine.dispose()


def test_dry_run_start_reaches_running(db):
    lab = make_lab(db)

    LabManager().start_lab_instance(lab, db)
    db.refresh(lab)

    assert lab.status == "running"
    assert lab.instance_id.startswith("i-dryrun-")
    assert lab.private_ip == "10.0.1.100"
    assert lab.security_group_id == "sg-dryrun123"
    assert lab.error_message is None
    assert lab.terraform_state_path is not None


def test_each_lab_gets_own_workspace_and_state_key(db, tmp_path):
    lab_one = make_lab(db, "one")
    lab_two = make_lab(db, "two")

    lm = LabManager()
    lm.start_lab_instance(lab_one, db)
    lm.start_lab_instance(lab_two, db)

    assert lab_one.terraform_state_path != lab_two.terraform_state_path
    assert lm.lab_state_key(lab_one) != lm.lab_state_key(lab_two)
    assert lm.lab_state_key(lab_one) == f"labs/{lab_one.id}/terraform.tfstate"
    assert lm.lab_state_key(lab_two) == f"labs/{lab_two.id}/terraform.tfstate"
    assert Path(lab_one.terraform_state_path).is_dir()
    assert Path(lab_two.terraform_state_path).is_dir()
    assert str(tmp_path) in lab_one.terraform_state_path


def test_lab_workspace_uses_lab_terraform_source_and_tags(db):
    lab = make_lab(db)

    LabManager().start_lab_instance(lab, db)

    workspace = Path(lab.terraform_state_path)
    tfvars = (workspace / "terraform.tfvars").read_text()
    backend = (workspace / "backend.tf").read_text()

    assert '"ResourceType": "lab"' in tfvars
    assert f'"LabInstanceId": "lab-{lab.id}"' in tfvars
    assert f'"LearnerId": "{lab.learner_id}"' in tfvars
    assert f'"RoomId": "{lab.room_id}"' in tfvars
    assert '"Environment": "learner-lab"' in tfvars
    assert '"ResourceType": "builder"' not in tfvars
    assert f"labs/{lab.id}/terraform.tfstate" in backend
    assert (workspace / "modules" / "lab_env" / "instance.tf").is_file()


def test_builder_flow_is_unchanged(db, tmp_path):
    creator, _learner, room = make_people(db, "builder")
    builder = BuilderEnvironment(
        room_id=room.id,
        creator_id=creator.id,
        status="pending",
        terraform_state_path="",
        expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    db.add(builder)
    db.flush()
    builder.terraform_state_path = str(
        tmp_path / "builders" / f"builder-{builder.id}"
    )
    db.commit()

    LabManager().start_builder(builder)
    db.commit()
    db.refresh(builder)

    workspace = Path(builder.terraform_state_path)
    tfvars = (workspace / "terraform.tfvars").read_text()
    backend = (workspace / "backend.tf").read_text()

    assert builder.status == "running"
    assert builder.instance_id.startswith("i-dryrun-")
    assert f"builders/{builder.id}/terraform.tfstate" in backend
    assert '"ResourceType": "builder"' in tfvars
    assert '"ResourceType": "lab"' not in tfvars
    assert str(tmp_path / "builders") in builder.terraform_state_path


def test_dry_run_never_invokes_terraform_binary_or_aws(db):
    lab = make_lab(db)
    lm = LabManager()

    with patch("app.lab_manager.subprocess.run") as run_mock:
        with patch("boto3.client") as client_mock:
            with patch("boto3.resource") as resource_mock:
                lm.start_lab_instance(lab, db)
                lm.stop_lab_instance(lab, db)

    run_mock.assert_not_called()
    client_mock.assert_not_called()
    resource_mock.assert_not_called()
    db.refresh(lab)
    assert lab.status == "stopped"


def test_start_failure_marks_lab_failed(db):
    lab = make_lab(db)

    with patch.object(LabManager, "_run_terraform", failing_command("apply")):
        with pytest.raises(TerraformError):
            LabManager().start_lab_instance(lab, db)

    db.refresh(lab)
    assert lab.status == "failed"
    assert lab.instance_id is None
    assert lab.error_message == "Terraform apply failed"


def test_error_message_is_sanitized_and_bounded(db):
    lab = make_lab(db)
    lm = LabManager()

    message = (
        "aws_secret_access_key=wJalrXUtnFEMIKbPxRfiCYEXAMPLEKEY\n"
        "aws_access_key_id=AKIAEXAMPLE12345678\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload\n"
        + "x" * 600
    )
    sanitized = lm._sanitize_error_message(message)

    assert "wJalrXUtnFEMIKbPxRfiCYEXAMPLEKEY" not in sanitized
    assert "AKIAEXAMPLE12345678" not in sanitized
    assert "eyJhbGciOiJIUzI1NiJ9.payload" not in sanitized
    assert "aws_secret_access_key=[REDACTED]" in sanitized
    assert len(sanitized) <= 501

    with patch.object(LabManager, "_run_terraform", failing_command("apply")):
        with pytest.raises(TerraformError):
            LabManager().start_lab_instance(lab, db)

    db.refresh(lab)
    assert lab.error_message is not None
    assert len(lab.error_message) <= 500


def test_dry_run_stop_reaches_stopped(db):
    lab = make_lab(db)
    lm = LabManager()
    lm.start_lab_instance(lab, db)

    lm.stop_lab_instance(lab, db)
    db.refresh(lab)

    assert lab.status == "stopped"
    assert lab.instance_id is None
    assert lab.private_ip is None
    assert lab.security_group_id is None
    assert lab.error_message is None
    assert lab.stopped_at is not None
    assert not Path(lab.terraform_state_path).exists()


def test_destroy_failure_preserves_stopping_and_keeps_resources(db):
    lab = make_lab(db)
    lm = LabManager()
    lm.start_lab_instance(lab, db)
    instance_id = lab.instance_id

    with patch.object(
        LabManager, "_run_terraform", failing_command("destroy")
    ):
        with pytest.raises(TerraformError):
            lm.stop_lab_instance(lab, db)

    db.refresh(lab)
    assert lab.status == "stopping"
    assert lab.instance_id == instance_id
    assert lab.private_ip is not None
    assert lab.security_group_id is not None
    assert lab.stopped_at is None
    assert "Cleanup retry required: destroy failed" in lab.error_message
    assert Path(lab.terraform_state_path).exists()


def test_stop_reconstructs_workspace_when_missing(db):
    lab = make_lab(db)
    lm = LabManager()
    lm.start_lab_instance(lab, db)

    shutil.rmtree(lab.terraform_state_path)

    lm.stop_lab_instance(lab, db)
    db.refresh(lab)

    assert lab.status == "stopped"
    assert not Path(lab.terraform_state_path).exists()


def test_reconciliation_stops_expired_running_lab(db):
    lab = make_lab(
        db,
        status="running",
        instance_id="i-expired",
        private_ip="10.0.1.50",
        security_group_id="sg-expired",
        expires_at=datetime.utcnow() - timedelta(minutes=5),
    )
    worker = ReconciliationWorker()

    asyncio.run(worker._handle_expired_running_labs(db))
    db.refresh(lab)

    assert lab.status == "stopped"
    assert lab.instance_id is None
    assert lab.stopped_at is not None


def test_reconciliation_marks_stale_pending_lab_failed(db):
    lab = make_lab(
        db,
        status="pending",
        created_at=datetime.utcnow() - timedelta(minutes=30),
    )
    worker = ReconciliationWorker()

    asyncio.run(worker._handle_stale_pending_labs(db))
    db.refresh(lab)

    assert lab.status == "failed"
    assert "stale in pending" in lab.error_message


def test_reconciliation_retries_crashed_stopping_lab(db):
    lab = make_lab(
        db,
        status="stopping",
        instance_id="i-crashed",
        private_ip="10.0.1.60",
        security_group_id="sg-crashed",
        updated_at=datetime.utcnow() - (stopping_grace() + timedelta(seconds=1)),
    )
    worker = ReconciliationWorker()

    asyncio.run(worker._handle_crashed_stopping_labs(db))
    db.refresh(lab)

    assert lab.status == "stopped"
    assert lab.instance_id is None
    assert lab.stopped_at is not None


def test_guard_acquired_before_lab_stop_mutation(db):
    lab = make_lab(db, status="running", instance_id="i-run")
    with single_flight(lab_lock_key(lab.id)):
        with pytest.raises(OperationInProgress):
            LabManager().stop_lab_instance(lab, db)
    db.refresh(lab)
    assert lab.status == "running"
    assert lab.error_message is None


def test_guard_acquired_before_lab_start_mutation(db):
    lab = make_lab(db)
    with single_flight(lab_lock_key(lab.id)):
        with pytest.raises(OperationInProgress):
            LabManager().start_lab_instance(lab, db)
    db.refresh(lab)
    assert lab.status == "pending"
    assert lab.error_message is None


def test_guard_acquired_before_builder_stop_mutation(db, tmp_path):
    builder = make_builder(db, tmp_path, status="running")
    with single_flight(builder_lock_key(builder.id)):
        with pytest.raises(OperationInProgress):
            LabManager().stop_builder(builder)
    assert builder.status == "running"
    assert builder.error_message is None


def test_guard_acquired_before_builder_start_mutation(db, tmp_path):
    builder = make_builder(db, tmp_path, suffix="pend", status="pending")
    with single_flight(builder_lock_key(builder.id)):
        with pytest.raises(OperationInProgress):
            LabManager().start_builder(builder)
    assert builder.status == "pending"
    assert builder.error_message is None


def test_guard_released_after_lab_terraform_failure(db):
    lab = make_lab(db)
    with patch.object(LabManager, "_run_terraform", failing_command("apply")):
        with pytest.raises(TerraformError):
            LabManager().start_lab_instance(lab, db)
    db.refresh(lab)
    assert lab.status == "failed"
    with single_flight(lab_lock_key(lab.id)):
        pass


def test_guard_released_after_builder_terraform_failure(db, tmp_path):
    builder = make_builder(db, tmp_path, suffix="bfail")
    with patch.object(LabManager, "_run_terraform", failing_command("destroy")):
        with pytest.raises(TerraformError):
            LabManager().stop_builder(builder)
    assert builder.status == "stopping"
    with single_flight(builder_lock_key(builder.id)):
        pass


def test_reconciler_skips_fresh_stopping_lab(db):
    lab = make_lab(
        db, status="stopping", instance_id="i-fresh", private_ip="10.0.1.62",
        security_group_id="sg-fresh",
    )
    worker = ReconciliationWorker()
    calls = []
    with patch.object(LabManager, "_run_terraform", recording_terraform(calls)):
        asyncio.run(worker._handle_crashed_stopping_labs(db))
    db.refresh(lab)
    assert lab.status == "stopping"
    assert lab.error_message is None
    assert calls == []


def test_reconciler_skips_fresh_stopping_builder(db, tmp_path):
    builder = make_builder(db, tmp_path, suffix="fresh", status="stopping")
    worker = ReconciliationWorker()
    calls = []
    with patch.object(LabManager, "_run_terraform", recording_terraform(calls)):
        asyncio.run(worker._handle_crashed_stopping_builders(db))
    db.refresh(builder)
    assert builder.status == "stopping"
    assert calls == []


def test_guard_prevents_second_destroy_from_reconciler(db):
    lab = make_lab(
        db,
        status="stopping",
        instance_id="i-busy",
        private_ip="10.0.1.63",
        security_group_id="sg-busy",
        updated_at=datetime.utcnow() - (stopping_grace() + timedelta(seconds=1)),
    )
    worker = ReconciliationWorker()
    calls = []
    with patch.object(LabManager, "_run_terraform", recording_terraform(calls)):
        with single_flight(lab_lock_key(lab.id)):
            asyncio.run(worker._handle_crashed_stopping_labs(db))
        db.refresh(lab)
        assert lab.status == "stopping"
        assert lab.error_message is None
        assert calls == []
        lab.updated_at = datetime.utcnow() - (stopping_grace() + timedelta(seconds=1))
        db.commit()
        asyncio.run(worker._handle_crashed_stopping_labs(db))
        db.refresh(lab)
        assert lab.status == "stopped"
        assert any(c and c[0] == "destroy" for c in calls)
        assert any(c and c[0] == "init" for c in calls)


def test_guard_prevents_second_builder_destroy_from_reconciler(db, tmp_path):
    builder = make_builder(
        db,
        tmp_path,
        suffix="bbusy",
        status="stopping",
        updated_at=datetime.utcnow() - (stopping_grace() + timedelta(seconds=1)),
    )
    worker = ReconciliationWorker()
    calls = []
    with patch.object(LabManager, "_run_terraform", recording_terraform(calls)):
        with single_flight(builder_lock_key(builder.id)):
            asyncio.run(worker._handle_crashed_stopping_builders(db))
        db.refresh(builder)
        assert builder.status == "stopping"
        assert builder.error_message is None
        assert calls == []
        builder.updated_at = datetime.utcnow() - (stopping_grace() + timedelta(seconds=1))
        db.commit()
        asyncio.run(worker._handle_crashed_stopping_builders(db))
        db.refresh(builder)
        assert builder.status == "stopped"
        assert any(c and c[0] == "destroy" for c in calls)


def test_background_destroy_and_reconciler_share_single_flight(
    db_file, monkeypatch
):
    maker = db_file
    monkeypatch.setattr("app.labs.SessionLocal", maker)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_rec(self, cmd, **kwargs):
        calls.append(cmd)
        if cmd and cmd[0] == "destroy":
            started.set()
            if not release.wait(5):
                raise RuntimeError("test timeout waiting to release slow destroy")
        return ORIGINAL_RUN_TERRAFORM(self, cmd, **kwargs)

    with maker() as session:
        _, learner, room = make_people(session, "race")
        lab = LabInstance(
            room_id=room.id,
            learner_id=learner.id,
            status="stopping",
            instance_id="i-race",
            private_ip="10.0.1.64",
            security_group_id="sg-race",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        session.add(lab)
        session.commit()
        lab_id = lab.id

    worker = ReconciliationWorker()
    with patch.object(LabManager, "_run_terraform", slow_rec):
        background = threading.Thread(target=destroy_lab_background, args=(lab_id,))
        background.start()
        assert started.wait(5), "background destroy never started"

        with maker() as session:
            row = session.get(LabInstance, lab_id)
            row.updated_at = datetime.utcnow() - (
                stopping_grace() + timedelta(seconds=1)
            )
            session.commit()
            asyncio.run(worker._handle_crashed_stopping_labs(session))
            session.refresh(row)
            assert row.status == "stopping"
            assert row.error_message is None

        release.set()
        background.join(timeout=10)
        assert not background.is_alive(), "background destroy thread did not finish"

        with maker() as session:
            row = session.get(LabInstance, lab_id)
            assert row.status == "stopped"
            assert row.error_message is None

    destroy_count = sum(1 for c in calls if c and c[0] == "destroy")
    assert destroy_count == 1


def test_operation_in_progress_never_marks_lab_failed(db):
    lab = make_lab(
        db,
        status="stopping",
        instance_id="i-race",
        updated_at=datetime.utcnow() - (stopping_grace() + timedelta(seconds=1)),
    )
    worker = ReconciliationWorker()
    with patch.object(
        LabManager, "stop_lab_instance", side_effect=OperationInProgress("busy")
    ):
        asyncio.run(worker._handle_crashed_stopping_labs(db))
    db.refresh(lab)
    assert lab.status == "stopping"
    assert lab.error_message is None


def test_normal_stop_behavior_unchanged(db):
    lab = make_lab(db)
    LabManager().start_lab_instance(lab, db)
    calls = []
    with patch.object(LabManager, "_run_terraform", recording_terraform(calls)):
        LabManager().stop_lab_instance(lab, db)
    db.refresh(lab)
    assert lab.status == "stopped"
    assert lab.error_message is None
    assert lab.stopped_at is not None
    assert sum(1 for c in calls if c and c[0] == "destroy") == 1


def test_normal_builder_stop_behavior_unchanged(db, tmp_path):
    builder = make_builder(db, tmp_path, suffix="nbuilder")
    calls = []
    with patch.object(LabManager, "_run_terraform", recording_terraform(calls)):
        LabManager().stop_builder(builder)
    assert builder.status == "stopped"
    assert builder.error_message is None
    assert sum(1 for c in calls if c and c[0] == "destroy") == 1


def test_mark_stopped_does_not_revert_newer_failed(db):
    lab = make_lab(db, status="failed", error_message="newer error")
    worker = ReconciliationWorker()
    worker._mark_lab_stopped(db, lab.id)
    db.refresh(lab)
    assert lab.status == "failed"
    assert lab.error_message == "newer error"


def test_mark_failed_does_not_revert_stopped(db):
    lab = make_lab(db, status="stopped")
    worker = ReconciliationWorker()
    worker._mark_lab_failed(db, lab.id, "late failure")
    db.refresh(lab)
    assert lab.status == "stopped"


@pytest.mark.parametrize("apply_fails", [False, True])
def test_stop_during_apply_survives_and_reconciliation_destroys(
    db_file, monkeypatch, apply_fails
):
    maker = db_file
    monkeypatch.setattr("app.labs.SessionLocal", maker)
    monkeypatch.setattr("app.lab_manager.sync_vpn_authz", lambda db: None)
    held = threading.Event()
    resume = threading.Event()
    active = set()
    active_lock = threading.Lock()
    calls = []
    errors = []

    def terraform(self, cmd, **kwargs):
        verb = cmd[0]
        with active_lock:
            assert not active, "Terraform provisioning and destruction overlapped"
            active.add(verb)
            calls.append(verb)
        try:
            if verb == "apply":
                (Path(kwargs["cwd"]) / "partial-state-marker").write_text("offline state")
                held.set()
                assert resume.wait(10), "test failed to release provisioning"
                if apply_fails:
                    return subprocess.CompletedProcess(cmd, 1, "", "offline apply failure")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        finally:
            with active_lock:
                active.remove(verb)

    monkeypatch.setattr(LabManager, "_run_terraform", terraform)
    monkeypatch.setattr(LabManager, "_parse_terraform_outputs", lambda *args: {
        "instance_id": "i-offline", "private_ip": "10.0.0.20",
        "security_group_id": "sg-offline",
    })
    with maker() as db:
        lab = make_lab(db, suffix="stop-apply")
        lab_id, room_id, learner_id = lab.id, lab.room_id, lab.learner_id

    def provision():
        try:
            with maker() as db:
                manager = LabManager()
                # Exercise real workspace cleanup decisions; all Terraform is fake.
                manager.config["terraform_dry_run"] = False
                manager.start_lab_instance(db.get(LabInstance, lab_id), db)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=provision)
    thread.start()
    try:
        assert held.wait(10), "provisioning did not reach apply"
        with maker() as db:
            lab = db.get(LabInstance, lab_id)
            assert lab.status == "provisioning"
            workspace = Path(lab.terraform_state_path)
            assert workspace.exists()
            tasks = BackgroundTasks()
            stopped = stop_lab(room_id, tasks, db.get(User, learner_id), db)
            assert stopped.status == "stopping"
            assert len(tasks.tasks) == 1
            assert tasks.tasks[0].func is destroy_lab_background

        # Execute the actual scheduled task while the provisioning lock is held.
        asyncio.run(tasks())
        assert "destroy" not in calls
        with maker() as db:
            assert db.get(LabInstance, lab_id).status == "stopping"
            repeated = BackgroundTasks()
            assert stop_lab(room_id, repeated, db.get(User, learner_id), db).status == "stopping"
            assert repeated.tasks == []
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive()
    if apply_fails:
        assert len(errors) == 1 and isinstance(errors[0], TerraformError)
    else:
        assert errors == []
    assert workspace.exists()
    assert (workspace / "partial-state-marker").exists()
    with maker() as db:
        lab = db.get(LabInstance, lab_id)
        assert lab.status == "stopping"
        assert lab.terraform_state_path == str(workspace)
        if apply_fails:
            assert lab.error_message == "Terraform apply failed"
        else:
            assert lab.instance_id == "i-offline"
        # Advance eligibility explicitly; no sleeps or wall-clock race.
        lab.updated_at = datetime.utcnow() - stopping_grace() - timedelta(seconds=1)
        db.commit()

    worker = ReconciliationWorker()
    with maker() as db:
        asyncio.run(worker._handle_crashed_stopping_labs(db))
        db.refresh(lab := db.get(LabInstance, lab_id))
        assert lab.status == "stopped"
        assert lab.instance_id is None
        assert lab.stopped_at is not None
    assert calls.count("destroy") == 1
    assert not active
    assert not workspace.exists()
    # A second scheduled task and recovery pass must not repeat destruction.
    destroy_lab_background(lab_id)
    with maker() as db:
        asyncio.run(worker._handle_crashed_stopping_labs(db))
    assert calls.count("destroy") == 1


@pytest.mark.parametrize("authorization_fails", [False, True])
def test_stop_during_authorization_wins_final_provisioning_update(
    db_file, monkeypatch, authorization_fails
):
    maker = db_file
    with maker() as db:
        lab = make_lab(db, suffix="stop-auth")
        lab_id, room_id, learner_id = lab.id, lab.room_id, lab.learner_id

    def authorize(db, lab):
        # The provisioning session still sees provisioning. Another transaction
        # accepts Stop at the last seam before the conditional running update.
        with maker() as other:
            tasks = BackgroundTasks()
            stop_lab(room_id, tasks, other.get(User, learner_id), other)
        if authorization_fails:
            raise VpnAuthorizationError("offline authorization failure")

    monkeypatch.setattr("app.lab_manager.authorize_lab_access", authorize)
    with maker() as db:
        manager = LabManager()
        lab = db.get(LabInstance, lab_id)
        if authorization_fails:
            with pytest.raises(VpnAuthorizationError):
                manager.start_lab_instance(lab, db)
        else:
            manager.start_lab_instance(lab, db)
        db.refresh(lab)
        assert lab.status == "stopping"
        assert lab.instance_id is not None
        assert Path(lab.terraform_state_path).exists()


@pytest.mark.parametrize("new_status", ["failed", "stopped"])
def test_provisioning_completion_does_not_overwrite_newer_terminal_state(
    db_file, monkeypatch, new_status
):
    maker = db_file
    with maker() as db:
        lab = make_lab(db, suffix="terminal-completion")
        lab_id = lab.id

    def authorize(db, lab):
        with maker() as other:
            other.execute(update(LabInstance).where(LabInstance.id == lab_id).values(
                status=new_status, error_message="newer terminal decision",
            ))
            other.commit()

    monkeypatch.setattr("app.lab_manager.authorize_lab_access", authorize)
    with maker() as db:
        LabManager().start_lab_instance(db.get(LabInstance, lab_id), db)
        lab = db.get(LabInstance, lab_id)
        assert lab.status == new_status
        assert lab.error_message == "newer terminal decision"


def test_stale_destroy_and_reconciliation_claim_do_not_revive_stopped_lab(db_file):
    maker = db_file
    with maker() as stale, maker() as other:
        lab = make_lab(stale, suffix="stale-destroy", status="stopping")
        lab_id = lab.id
        other.execute(update(LabInstance).where(LabInstance.id == lab_id).values(status="stopped"))
        other.commit()
        worker = ReconciliationWorker()
        assert worker._claim_lab_for_stop(stale, lab_id) is None
        with patch.object(LabManager, "_run_terraform") as terraform:
            LabManager().stop_lab_instance(lab, stale)
        terraform.assert_not_called()
        stale.refresh(lab)
        assert lab.status == "stopped"


def test_stop_api_conditional_update_losing_race_schedules_nothing(db_file, monkeypatch):
    maker = db_file
    with maker() as db:
        lab = make_lab(db, suffix="stop-claim", status="running")
        room_id, learner_id, lab_id = lab.room_id, lab.learner_id, lab.id
        original_execute = db.execute

        def lose_claim(statement, *args, **kwargs):
            if statement.is_update:
                with maker() as other:
                    other.execute(update(LabInstance).where(LabInstance.id == lab_id).values(status="stopped"))
                    other.commit()
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", lose_claim)
        tasks = BackgroundTasks()
        assert stop_lab(room_id, tasks, db.get(User, learner_id), db).status == "stopped"
        assert tasks.tasks == []
        db.refresh(lab)
        assert lab.status == "stopped"


def test_simultaneous_stop_requests_schedule_one_destroy(db_file, monkeypatch):
    maker = db_file
    monkeypatch.setattr("app.labs.SessionLocal", maker)
    monkeypatch.setattr("app.lab_manager.sync_vpn_authz", lambda db: None)
    with maker() as db:
        lab = make_lab(db, suffix="two-stops", status="running")
        lab_id, room_id, learner_id = lab.id, lab.room_id, lab.learner_id
    ready = threading.Barrier(2)
    results = []
    errors = []

    def attempt():
        try:
            with maker() as db:
                original_execute = db.execute
                def simultaneous_claim(statement, *args, **kwargs):
                    if statement.is_update:
                        ready.wait(10)
                    return original_execute(statement, *args, **kwargs)
                db.execute = simultaneous_claim
                tasks = BackgroundTasks()
                try:
                    stop_lab(room_id, tasks, db.get(User, learner_id), db)
                    results.append((200, tasks))
                except HTTPException as exc:
                    results.append((exc.status_code, tasks))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert errors == []
    assert sorted(status for status, _ in results) == [200, 200]
    accepted = next(tasks for _, tasks in results if tasks.tasks)
    rejected = next(tasks for _, tasks in results if not tasks.tasks)
    assert len(accepted.tasks) == 1
    assert rejected.tasks == []
    calls = []
    with patch.object(LabManager, "_run_terraform", recording_terraform(calls)):
        asyncio.run(accepted())
        destroy_lab_background(lab_id)
    assert sum(cmd[0] == "destroy" for cmd in calls) == 1
    with maker() as db:
        assert db.get(LabInstance, lab_id).status == "stopped"


@pytest.mark.parametrize("new_status", ["failed", "stopped"])
def test_stale_provisioning_failure_preserves_newer_terminal_state(db_file, new_status):
    maker = db_file
    with maker() as stale, maker() as other:
        lab = make_lab(stale, suffix="stale-failure", status="provisioning")
        other.execute(update(LabInstance).where(LabInstance.id == lab.id).values(
            status=new_status, error_message="newer terminal decision",
        ))
        other.commit()
        assert not LabManager()._record_lab_provisioning_failure(stale, lab, "late failure")
        assert lab.status == new_status
        assert lab.error_message == "newer terminal decision"


@pytest.mark.parametrize("new_status", ["provisioning", "stopping", "stopped"])
def test_stale_pending_reconciliation_does_not_overwrite_newer_state(
    db_file, monkeypatch, new_status
):
    maker = db_file
    with maker() as db:
        lab = make_lab(db, suffix="pending-recovery")
        lab.created_at = datetime.utcnow() - timedelta(days=1)
        db.commit()
        # Materialize the stale selection, then commit a competing lifecycle
        # transition on another connection before reconciliation processes it.
        snapshot = db.scalars(select(LabInstance)).all()
        with maker() as other:
            other.execute(update(LabInstance).where(LabInstance.id == lab.id).values(status=new_status))
            other.commit()
        original_execute = db.execute
        def stale_selection(statement, *args, **kwargs):
            if statement.is_select:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: snapshot))
            return original_execute(statement, *args, **kwargs)
        monkeypatch.setattr(db, "execute", stale_selection)
        asyncio.run(ReconciliationWorker()._handle_stale_pending_labs(db))
        monkeypatch.setattr(db, "execute", original_execute)
        db.refresh(lab)
        assert lab.status == new_status


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
