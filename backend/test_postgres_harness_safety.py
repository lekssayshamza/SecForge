"""Offline safety checks only. No server, DB connection, or external process."""

import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from test_support.postgres_harness import (
    DisposablePostgres, HarnessError, TEST_DATABASE, TEST_ROLE,
    offline_guards, validate_port,
)


@pytest.fixture
def owned(tmp_path):
    cluster = DisposablePostgres(55439)
    cluster.root = tmp_path / "owned"
    cluster.root.mkdir(mode=0o700)
    cluster._identity = cluster._directory_identity(cluster.root)
    (cluster.root / "owner").write_text(cluster._token)
    (cluster.root / "owner").chmod(0o600)
    for name in ("data", "socket", "work"):
        path = cluster.root / name
        path.mkdir(mode=0o700)
        cluster._children[name] = cluster._directory_identity(path)
    return cluster


@pytest.mark.parametrize("port", [5432, 0, 1023, 65536, "55439", True, None])
def test_refuses_default_or_ambiguous_ports(port):
    with pytest.raises(HarnessError):
        validate_port(port)


def test_explicit_nondefault_port():
    assert validate_port(55439) == 55439


def test_import_does_not_start_and_cli_requires_opt_in(monkeypatch):
    import run_postgres_concurrency
    called = []
    monkeypatch.setattr(DisposablePostgres, "allocate", lambda self: called.append("allocate"))
    for arguments in ([], ["--port", "55439"], ["--allow-disposable-postgres"],
                      ["--allow-disposable-postgres", "--port", "55439", "--url", "refused"]):
        with pytest.raises(SystemExit) as exc:
            run_postgres_concurrency.main(arguments)
        assert exc.value.code == 2
    assert called == []


@pytest.mark.parametrize("change", [
    {"host": "127.0.0.1"}, {"host": "/var/run/postgresql"}, {"port": 5432},
    {"dbname": "secforge"}, {"user": "postgres"}, {"service": "development"},
    {"hostaddr": "127.0.0.1"}, {"password": "not-used"},
])
def test_refuses_nonowned_connection_targets(owned, monkeypatch, change):
    calls = []
    monkeypatch.setattr(owned, "assert_server", lambda: calls.append("checked"))
    parameters = dict(host=str(owned.socket), port=owned.port, dbname=TEST_DATABASE, user=TEST_ROLE)
    parameters.update(change)
    with pytest.raises(HarnessError):
        owned.validate_connection(parameters)
    assert calls == []  # Refused before even inspecting or connecting to a server.


@pytest.mark.parametrize("missing", ["host", "port", "dbname", "user"])
def test_connection_parameters_cannot_fall_back_to_environment(owned, monkeypatch, missing):
    monkeypatch.setattr(owned, "assert_server", lambda: None)
    parameters = dict(host=str(owned.socket), port=owned.port, dbname=TEST_DATABASE, user=TEST_ROLE)
    del parameters[missing]
    with pytest.raises(HarnessError):
        owned.validate_connection(parameters)


def test_sqlalchemy_constructs_explicit_private_socket_parameters_offline(owned, monkeypatch):
    from sqlalchemy import create_engine
    import psycopg
    calls = []
    monkeypatch.setattr(owned, "assert_server", lambda: None)
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: calls.append(kwargs))
    engine = create_engine(owned.sqlalchemy_url())
    try:
        args, parameters = engine.dialect.create_connect_args(engine.url)
        assert args == []
        # SQLAlchemy also supplies a non-libpq AdaptersMap context. Exercise the
        # actual wrapper, which separates that DBAPI option from target options.
        with offline_guards(owned):
            psycopg.connect(*args, **parameters)
        assert len(calls) == 1 and calls[0]["host"] == str(owned.socket)
    finally:
        engine.dispose()  # No connection was made.


@pytest.mark.parametrize("kind", ["marker", "mode", "child_symlink", "root_symlink"])
def test_uncertain_ownership_refuses_cleanup(owned, tmp_path, kind):
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(mode=0o700)
    sentinel = unrelated / "preserve"
    sentinel.write_text("preserve")
    if kind == "marker":
        (owned.root / "owner").write_text("wrong owner")
    elif kind == "mode":
        owned.root.chmod(0o755)
    elif kind == "child_symlink":
        owned.data.rmdir()
        owned.data.symlink_to(unrelated, target_is_directory=True)
    else:
        owned.root.rename(tmp_path / "original-owned")
        owned.root.symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(HarnessError):
        owned.close()
    assert sentinel.read_text() == "preserve"


def test_close_unstarted_owned_directory_only(owned, tmp_path):
    unrelated = tmp_path / "keep"
    unrelated.write_text("keep")
    root = owned.root
    owned.close()
    assert not root.exists() and unrelated.read_text() == "keep"


def test_uncertain_start_retains_directory_without_control_commands(owned, monkeypatch):
    owned._start_attempted = True
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: called.append(a))
    with pytest.raises(HarnessError):
        owned.close()
    assert called == [] and owned.root.is_dir()


def test_control_rejects_other_data_directory_without_execution(owned, monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: called.append(a))
    with pytest.raises(HarnessError):
        owned._control("pg_ctl", ["-D", "/unrelated", "stop"])
    assert called == []


def test_start_plan_is_private_and_bootstrap_ignores_inherited_libpq(owned, monkeypatch):
    import psycopg
    controls, statements, connections = [], [], []
    monkeypatch.setenv("PGHOSTADDR", "refused")
    monkeypatch.setenv("PGSERVICE", "refused")
    monkeypatch.setenv("DATABASE_URL", "refused")
    monkeypatch.setattr(owned, "_control", lambda name, args: controls.append((name, args)))
    monkeypatch.setattr(owned, "assert_server", lambda: None)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql):
            statements.append(sql)
            self.last = sql
            return self

        def fetchone(self):
            return (str(owned.data),) if self.last == "SHOW data_directory" else (None,)

    def connect(**kwargs):
        assert not any(key in os.environ for key in ("PGHOSTADDR", "PGSERVICE", "DATABASE_URL"))
        connections.append(kwargs)
        return FakeConnection()

    monkeypatch.setattr(psycopg, "connect", connect)
    owned.start()  # Both control commands and DBAPI are fakes; no server is run.
    assert [name for name, _ in controls] == ["initdb", "pg_ctl"]
    for _, arguments in controls:
        assert arguments[arguments.index("-D") + 1] == str(owned.data)
    start = controls[1][1]
    options = start[start.index("-o") + 1]
    assert "listen_addresses=''" in options and "unix_socket_permissions=0700" in options
    assert str(owned.socket) in options and str(owned.port) in options
    assert start[start.index("-t") + 1] == "20"
    assert len(connections) == 1 and connections[0]["host"] == str(owned.socket)
    assert any("NOSUPERUSER NOCREATEDB NOCREATEROLE" in sql for sql in statements)
    assert any(f"CREATE DATABASE {TEST_DATABASE} OWNER {TEST_ROLE}" == sql for sql in statements)
    assert sum(sql.startswith("REVOKE CONNECT") for sql in statements) == 3
    assert owned.started


def test_failed_shutdown_retains_owned_files(owned, monkeypatch):
    owned._start_attempted = True
    monkeypatch.setattr(owned, "assert_server", lambda: None)

    def failed(*args):
        raise HarnessError("fake failed shutdown")

    monkeypatch.setattr(owned, "_control", failed)
    with pytest.raises(HarnessError):
        owned.close()
    assert owned.root.is_dir()


def test_failed_initialization_retains_files_without_guessing_cleanup(owned, monkeypatch):
    def failed(*args):
        raise HarnessError("fake initdb timeout")

    monkeypatch.setattr(owned, "_control", failed)
    with pytest.raises(HarnessError):
        owned.start()
    with pytest.raises(HarnessError, match="Initialization uncertain"):
        owned.close()
    assert owned.root.is_dir() and not owned._start_attempted


def test_runner_preserves_pytest_failure_status_and_safe_environment(owned, monkeypatch):
    import run_postgres_concurrency
    from test_support import postgres_harness
    monkeypatch.setenv("DATABASE_URL", "refused")
    monkeypatch.setenv("PGSERVICE", "refused")
    monkeypatch.setattr(postgres_harness, "DisposablePostgres", lambda port: owned)
    monkeypatch.setattr(owned, "allocate", lambda: None)
    monkeypatch.setattr(owned, "start", lambda: setattr(owned, "started", True))
    called = []

    def fake_pytest(arguments):
        # The integration test module is NOT executed in this offline check.
        called.append(arguments)
        assert os.environ["DATABASE_URL"] != "refused"
        assert "PGSERVICE" not in os.environ
        assert Path.cwd() == owned.root / "work"
        assert postgres_harness.ACTIVE_CLUSTER is owned
        return 1

    monkeypatch.setattr(pytest, "main", fake_pytest)
    assert run_postgres_concurrency.main(["--allow-disposable-postgres", "--port", "55439"]) == 1
    assert len(called) == 1 and "test_postgres_lab_concurrency.py" in called[0][0]
    assert postgres_harness.ACTIVE_CLUSTER is None and owned.root is None


def test_guard_denies_network_aws_processes_and_foreign_libpq(owned, monkeypatch):
    import boto3
    import psycopg
    import socket
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: calls.append("libpq"))
    with offline_guards(owned):
        operations = [
            lambda: psycopg.connect(host="127.0.0.1", port=5432, dbname="refused", user="refused"),
            lambda: subprocess.run(["refused"]),
            lambda: boto3.client("ec2"),
            lambda: boto3.session.Session.client(None, "ec2"),
        ]
        for operation in operations:
            with pytest.raises(HarnessError):
                operation()
        with pytest.raises(HarnessError):
            socket.socket.connect(None, ("127.0.0.1", 1))
        # asyncio's local wakeup socket and thread completion remain usable.
        async def complete():
            return await asyncio.wait_for(asyncio.to_thread(lambda: "complete"), timeout=2)
        assert asyncio.run(complete()) == "complete"
    assert calls == []
