"""Opt-in PostgreSQL lifecycle tests; run only through the disposable runner.

Normal pytest collection skips this module before application imports. The
in-process capability below cannot be enabled by a DATABASE_URL or environment
flag. No existing SQLite test or fixture is redirected.
"""

import pytest
from test_support import postgres_harness as harness

if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip("Requires run_postgres_concurrency.py and its owned cluster", allow_module_level=True)

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import threading
import time
import uuid

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import database, labs, reconciliation
from app.config import settings
from app.lab_manager import LabManager
from app.models import LabInstance, User
from app.schemas import LabStartRequest
import test_lab_instance_orchestration as sqlite_cases


@pytest.fixture
def pg_sessions(monkeypatch, tmp_path):
    cluster = harness.ACTIVE_CLUSTER
    cluster.assert_server()
    schema = "case_" + uuid.uuid4().hex
    control = create_engine(cluster.sqlalchemy_url(), connect_args={
        "connect_timeout": 5, "options": "-c statement_timeout=20000 -c lock_timeout=5000",
    })
    engine = None
    try:
        with control.begin() as connection:
            assert connection.scalar(text("SELECT current_database()")) == harness.TEST_DATABASE
            assert connection.scalar(text("SELECT current_user")) == harness.TEST_ROLE
            assert connection.scalar(text("SELECT inet_server_addr()")) is None
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        # Each Session checks out its own real connection; no enclosing test
        # transaction, shared connection, SQLite pool, or transaction emulation.
        engine = create_engine(cluster.sqlalchemy_url(), connect_args={
            "connect_timeout": 5,
            "options": f"-c search_path={schema} -c statement_timeout=20000 -c lock_timeout=15000",
        })
        database.Base.metadata.create_all(engine)
        maker = sessionmaker(bind=engine, autoflush=False)
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(database, "SessionLocal", maker)
        monkeypatch.setattr(labs, "SessionLocal", maker)
        monkeypatch.setattr(reconciliation, "SessionLocal", maker)
        monkeypatch.setattr(settings, "terraform_dry_run", True)
        monkeypatch.setattr(settings, "lab_terraform_workspace_root", str(tmp_path / "labs"))
        monkeypatch.setattr(settings, "builder_terraform_workspace_root", str(tmp_path / "builders"))
        monkeypatch.setattr("app.lab_manager.sync_vpn_authz", lambda db: None)
        monkeypatch.setattr("app.lab_manager.authorize_lab_access", lambda db, lab: None)
        monkeypatch.setattr("app.reconciliation.sync_vpn_authz", lambda db: None)

        def prepare(self, lab, *args, **kwargs):
            path = Path(lab.terraform_state_path)
            assert path.is_relative_to(tmp_path)
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        monkeypatch.setattr(LabManager, "_prepare_lab_workspace", prepare)
        monkeypatch.setattr(LabManager, "_run_terraform", lambda self, cmd, **kw:
                            subprocess.CompletedProcess(cmd, 0, "", ""))
        monkeypatch.setattr(LabManager, "_parse_terraform_outputs", lambda *args: {
            "instance_id": "i-offline", "private_ip": "10.0.0.20", "security_group_id": "sg-offline",
        })
        yield maker
    finally:
        if engine is not None:
            engine.dispose()
        # Only this randomly named schema in this owned, disposable DB is dropped.
        try:
            with control.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            control.dispose()


@pytest.mark.parametrize("apply_fails", [False, True])
def test_stop_during_apply_and_deferred_reconciliation(pg_sessions, monkeypatch, apply_fails):
    # Reuse the existing event-controlled interleaving and all of its assertions:
    # committed provisioning -> held apply -> Stop -> locked destroy deferral ->
    # completion -> preserved stopping/workspace -> one reconciliation destroy.
    sqlite_cases.test_stop_during_apply_survives_and_reconciliation_destroys(
        pg_sessions, monkeypatch, apply_fails,
    )


@pytest.mark.parametrize("authorization_fails", [False, True])
def test_stop_wins_conditional_completion(pg_sessions, monkeypatch, authorization_fails):
    sqlite_cases.test_stop_during_authorization_wins_final_provisioning_update(
        pg_sessions, monkeypatch, authorization_fails,
    )


@pytest.mark.parametrize("new_status", ["failed", "stopped"])
def test_newer_terminal_state_wins_completion(pg_sessions, monkeypatch, new_status):
    sqlite_cases.test_provisioning_completion_does_not_overwrite_newer_terminal_state(
        pg_sessions, monkeypatch, new_status,
    )


@pytest.mark.parametrize("new_status", ["failed", "stopped"])
def test_newer_terminal_state_wins_failure(pg_sessions, new_status):
    sqlite_cases.test_stale_provisioning_failure_preserves_newer_terminal_state(pg_sessions, new_status)


@pytest.mark.parametrize("new_status", ["provisioning", "stopping", "stopped"])
def test_stale_pending_selection_cannot_overwrite_newer_state(pg_sessions, monkeypatch, new_status):
    sqlite_cases.test_stale_pending_reconciliation_does_not_overwrite_newer_state(
        pg_sessions, monkeypatch, new_status,
    )


def test_stale_pending_reconciliation_marks_failed(pg_sessions):
    with pg_sessions() as db:
        lab = sqlite_cases.make_lab(db, suffix="stale-pending")
        lab.created_at = datetime.utcnow() - timedelta(days=1)
        db.commit()
        asyncio.run(reconciliation.ReconciliationWorker()._handle_stale_pending_labs(db))
        db.refresh(lab)
        assert lab.status == "failed"


def test_stale_destroy_does_not_revive_terminal_lab(pg_sessions):
    sqlite_cases.test_stale_destroy_and_reconciliation_claim_do_not_revive_stopped_lab(pg_sessions)


def observe_lock_wait(maker, waiter, holder):
    """Order by server-observed blocking, never by an assumed sleep duration."""
    deadline = time.monotonic() + 6
    poll = threading.Event()
    with maker() as observer:
        assert observer.scalar(text("SELECT pg_backend_pid()")) not in {waiter, holder}
        while time.monotonic() < deadline:
            blockers = observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter})
            if holder in blockers:
                return
            poll.wait(0.01)  # Bounded observation polling, not an ordering assumption.
    pytest.fail("PostgreSQL did not report the expected row-lock wait")


def test_concurrent_stop_requests_serialize_on_postgres_row_lock(pg_sessions):
    with pg_sessions() as db:
        lab = sqlite_cases.make_lab(db, suffix="concurrent-stop", status="running")
        lab_id, room_id, learner_id = lab.id, lab.room_id, lab.learner_id
    held, release, second_ready = threading.Event(), threading.Event(), threading.Event()
    pids, responses, errors = {}, {}, []

    def actor(name):
        try:
            with pg_sessions() as db:
                pids[name] = db.scalar(text("SELECT pg_backend_pid()"))
                user = db.get(User, learner_id)
                if name == "first":
                    original_scalar = db.scalar

                    def scalar(statement, *args, **kwargs):
                        result = original_scalar(statement, *args, **kwargs)
                        if getattr(statement, "_for_update_arg", None) is not None:
                            assert result is not None
                            held.set()  # SELECT FOR UPDATE has actually returned.
                            assert release.wait(10), "first Stop was not released"
                        return result

                    db.scalar = scalar
                else:
                    second_ready.set()
                tasks = BackgroundTasks()
                try:
                    result = labs.stop_lab(room_id, tasks, user, db)
                    responses[name] = (200, result.status, tasks)
                except HTTPException as exc:
                    responses[name] = (exc.status_code, None, tasks)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=actor, args=("first",))
    second = threading.Thread(target=actor, args=("second",))
    first.start()
    try:
        assert held.wait(8), "first Stop did not acquire the row lock"
        second.start()
        assert second_ready.wait(8)
        assert pids["first"] != pids["second"]
        observe_lock_wait(pg_sessions, pids["second"], pids["first"])
        assert "second" not in responses  # It cannot complete while the lock is held.
    finally:
        release.set()
        first.join(22)
        if second.ident is not None:
            second.join(22)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert responses["first"][:2] == (200, "stopping")
    assert len(responses["first"][2].tasks) == 1
    assert responses["second"][:2] == (200, "stopping")
    assert responses["second"][2].tasks == []
    asyncio.run(responses["first"][2]())
    with pg_sessions() as db:
        assert db.get(LabInstance, lab_id).status == "stopped"


def test_concurrent_learner_creation_enforces_partial_unique_index(pg_sessions):
    with pg_sessions() as db:
        _, learner, room = sqlite_cases.make_people(db, suffix="concurrent-create")
        db.commit()
        learner_id, room_id = learner.id, room.id
    barrier = threading.Barrier(2, timeout=8)
    responses, errors, pids = [], [], []

    def actor():
        try:
            with pg_sessions() as db:
                pids.append(db.scalar(text("SELECT pg_backend_pid()")))
                # Access changes and launches now share a user-row lock. Start
                # both requests together; forcing both past that lock deadlocks
                # the harness rather than modeling a possible SQL interleaving.
                barrier.wait()
                tasks = BackgroundTasks()
                try:
                    lab = labs.start_lab(room_id, LabStartRequest(ttl_hours=1), tasks,
                                         db.get(User, learner_id), db)
                    responses.append((201, lab.id, tasks))
                except HTTPException as exc:
                    responses.append((exc.status_code, None, tasks))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=actor) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(22)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == [] and len(set(pids)) == 2
    assert sorted(response[0] for response in responses) == [201, 409]
    assert [len(response[2].tasks) for response in responses if response[0] == 201] == [1]
    assert [response[2].tasks for response in responses if response[0] == 409] == [[]]
    with pg_sessions() as db:
        rows = db.scalars(select(LabInstance)).all()
        assert len(rows) == 1 and rows[0].status == "pending"
        assert rows[0].terraform_state_path is None


def test_model_partial_index_is_real_and_excludes_terminal_rows(pg_sessions):
    with pg_sessions() as db:
        lab = sqlite_cases.make_lab(db, suffix="index-enforcement", status="stopped")
        room_id, learner_id = lab.room_id, lab.learner_id
        db.add(LabInstance(room_id=room_id, learner_id=learner_id, status="pending",
                           expires_at=datetime.utcnow() + timedelta(hours=1)))
        db.commit()  # A terminal row does not prevent a new active lab.
        definition = db.scalar(text("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                                    "AND indexname='ix_one_active_lab_per_learner_room'"))
        assert "UNIQUE INDEX" in definition and "WHERE" in definition
        db.add(LabInstance(room_id=room_id, learner_id=learner_id, status="running",
                           expires_at=datetime.utcnow() + timedelta(hours=1)))
        with pytest.raises(IntegrityError) as exc:
            db.commit()
        assert exc.value.orig.diag.constraint_name == "ix_one_active_lab_per_learner_room"
        db.rollback()


def test_conditional_update_loses_to_committed_stop(pg_sessions):
    with pg_sessions() as stale:
        lab = sqlite_cases.make_lab(stale, suffix="lost-update", status="provisioning")
        lab_id = lab.id
        with pg_sessions() as other:
            tasks = BackgroundTasks()
            labs.stop_lab(lab.room_id, tasks, other.get(User, lab.learner_id), other)
        result = stale.execute(update(LabInstance).where(
            LabInstance.id == lab_id, LabInstance.status == "provisioning",
        ).values(status="running"))
        stale.commit()
        assert result.rowcount == 0
        stale.refresh(lab)
        assert lab.status == "stopping"
