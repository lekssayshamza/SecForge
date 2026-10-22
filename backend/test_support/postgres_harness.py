"""Owned, disposable PostgreSQL support. Nothing starts on import.

Only run_postgres_concurrency.py may activate this harness. There is deliberately
no DSN/environment-based opt-in and no application DATABASE_URL fallback.
"""

import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

ACTIVE_CLUSTER = None
PG_BIN = Path("/usr/lib/postgresql/18/bin")
BOOTSTRAP_ROLE = "sf_test_bootstrap"
TEST_ROLE = "sf_concurrency_role"
TEST_DATABASE = "sf_concurrency_test"


class HarnessError(RuntimeError):
    pass


def validate_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 or port == 5432:
        raise HarnessError("An explicit non-default unprivileged port is required")
    return port


class DisposablePostgres:
    def __init__(self, port):
        self.port = validate_port(port)
        self.root = None
        self._identity = None
        self._children = {}
        self._token = secrets.token_hex(24)
        self._initialization_attempted = False
        self._initialized = False
        self._start_attempted = False
        self.started = False

    def allocate(self):
        if self.root is not None or os.geteuid() == 0:
            raise HarnessError("Allocation requires a non-root user and a fresh harness")
        for name in ("initdb", "pg_ctl", "postgres"):
            if not os.access(PG_BIN / name, os.X_OK):
                raise HarnessError("Required PostgreSQL 18 binaries are unavailable")
        # Ignore TMPDIR: the socket must fit PostgreSQL's Unix path length limit.
        self.root = Path(tempfile.mkdtemp(prefix="secforge-pg-test-", dir="/tmp"))
        self.root.chmod(0o700)
        self._identity = self._directory_identity(self.root)
        marker = self.root / "owner"
        marker.write_text(self._token)
        marker.chmod(0o600)
        for name in ("data", "socket", "work"):
            directory = self.root / name
            directory.mkdir(mode=0o700)
            self._children[name] = self._directory_identity(directory)
        self.assert_owned()

    @property
    def data(self):
        return self.root / "data"

    @property
    def socket(self):
        return self.root / "socket"

    @staticmethod
    def _directory_identity(path):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise HarnessError("Private directory ownership cannot be established")
        return info.st_dev, info.st_ino

    def assert_owned(self):
        if self.root is None or self._directory_identity(self.root) != self._identity:
            raise HarnessError("Temporary root ownership changed; refusing operation")
        marker = self.root / "owner"
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise HarnessError("Invalid ownership marker")
        if marker.read_text() != self._token:
            raise HarnessError("Ownership marker mismatch")
        for name, identity in self._children.items():
            if self._directory_identity(self.root / name) != identity:
                raise HarnessError("Owned directory replaced; refusing operation")

    def _control(self, binary, arguments):
        self.assert_owned()
        if binary not in {"initdb", "pg_ctl"}:
            raise HarnessError("Non-control program refused")
        try:
            data_argument = arguments[arguments.index("-D") + 1]
        except (ValueError, IndexError):
            raise HarnessError("Explicit owned data directory is required") from None
        if data_argument != str(self.data):
            raise HarnessError("External data directory refused")
        # Never forward PG*, database URLs, credentials, or inherited pg service files.
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        try:
            result = subprocess.run(
                [str(PG_BIN / binary), *arguments], cwd=self.root,
                env=environment, capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise HarnessError("Owned PostgreSQL control command failed or timed out") from None
        if result.returncode:
            # Server/control output is intentionally not echoed.
            raise HarnessError("Owned PostgreSQL control command failed")

    def assert_server(self):
        self.assert_owned()
        pidfile = self.data / "postmaster.pid"
        if pidfile.is_symlink():
            raise HarnessError("Unexpected server identity file")
        try:
            lines = pidfile.read_text().splitlines()
            pid = int(lines[0])
            matches = lines[1] == str(self.data) and int(lines[3]) == self.port and lines[4] == str(self.socket)
            proc = Path("/proc") / str(pid)
            command = (proc / "cmdline").read_bytes().split(b"\0")
            owned_process = proc.stat().st_uid == os.geteuid()
            executable = command[0].decode() == str(PG_BIN / "postgres")
            data_arg = command.index(b"-D") + 1
            matches = matches and command[data_arg].decode() == str(self.data)
        except (OSError, ValueError, IndexError):
            raise HarnessError("Server ownership cannot be established") from None
        if not (matches and owned_process and executable):
            raise HarnessError("Server ownership mismatch")
        endpoint = self.socket / f".s.PGSQL.{self.port}"
        info = endpoint.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise HarnessError("Unexpected PostgreSQL socket")

    def validate_connection(self, parameters, *, bootstrap=False):
        # Require explicit values; libpq environment/service/default discovery is forbidden.
        allowed = {"host", "port", "dbname", "user", "connect_timeout", "options", "application_name"}
        if set(parameters) - allowed:
            raise HarnessError("Unexpected PostgreSQL connection options")
        role = BOOTSTRAP_ROLE if bootstrap else TEST_ROLE
        database = "postgres" if bootstrap else TEST_DATABASE
        if parameters.get("host") != str(self.socket) or str(parameters.get("port")) != str(self.port):
            raise HarnessError("Only the owned private Unix socket is permitted")
        if parameters.get("dbname") != database or parameters.get("user") != role:
            raise HarnessError("Only the disposable database and dedicated role are permitted")
        self.assert_server()

    def start(self):
        if self.root is None or self._initialization_attempted:
            raise HarnessError("Allocate once before starting once")
        self._initialization_attempted = True
        self._control("initdb", ["-D", str(self.data), "-U", BOOTSTRAP_ROLE,
                                 "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8", "--no-locale"])
        self._initialized = True
        self._start_attempted = True
        self._control("pg_ctl", ["-D", str(self.data), "-l", str(self.root / "server.log"),
                                 "-w", "-t", "20", "-o",
                                 f"-k {self.socket} -p {self.port} -c listen_addresses='' -c unix_socket_permissions=0700",
                                 "start"])
        self.assert_server()
        import psycopg
        parameters = dict(host=str(self.socket), port=self.port, dbname="postgres",
                          user=BOOTSTRAP_ROLE, connect_timeout=5,
                          options="-c statement_timeout=20000 -c lock_timeout=5000")
        self.validate_connection(parameters, bootstrap=True)
        # Bootstrap libpq must also ignore inherited PGSERVICE/PGHOSTADDR/etc.,
        # before the application/test environment is installed by the runner.
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, clear=True):
            with psycopg.connect(**parameters, autocommit=True) as connection:
                if connection.execute("SHOW data_directory").fetchone()[0] != str(self.data):
                    raise HarnessError("Connected server data directory mismatch")
                if connection.execute("SELECT inet_server_addr()").fetchone()[0] is not None:
                    raise HarnessError("TCP connection refused")
                connection.execute(f"CREATE ROLE {TEST_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION")
                connection.execute(f"CREATE DATABASE {TEST_DATABASE} OWNER {TEST_ROLE}")
                for name in ("postgres", "template1", TEST_DATABASE):
                    connection.execute(f"REVOKE CONNECT ON DATABASE {name} FROM PUBLIC")
                connection.execute(f"GRANT CONNECT ON DATABASE {TEST_DATABASE} TO {TEST_ROLE}")
        self.started = True

    def close(self):
        if self.root is None:
            return
        self.assert_owned()
        if self._initialization_attempted and not self._initialized:
            # A timed-out initdb can leave a bootstrap child. No verified
            # postmaster identity is available, so do not delete or guess.
            raise HarnessError("Initialization uncertain; temporary files retained")
        if self._start_attempted:
            # Even failed startup can have launched a server. If identity is ambiguous,
            # retain the directory instead of guessing, killing, or deleting anything.
            self.assert_server()
            self._control("pg_ctl", ["-D", str(self.data), "-w", "-t", "20", "-m", "fast", "stop"])
            if (self.data / "postmaster.pid").exists() or (self.socket / f".s.PGSQL.{self.port}").exists():
                raise HarnessError("Shutdown uncertain; owned temporary files retained")
        self.assert_owned()
        shutil.rmtree(self.root)  # Root/children verified; rmtree does not follow child symlinks.
        self.root = None
        self.started = False

    def sqlalchemy_url(self):
        from sqlalchemy.engine import URL
        return URL.create("postgresql+psycopg", username=TEST_ROLE, database=TEST_DATABASE,
                          host=str(self.socket), port=self.port)


@contextmanager
def offline_guards(cluster):
    """Install before application imports; only validated libpq connections are allowed.

    Bounded selector polling handles restricted asyncio wakeup sockets. SQL,
    transactions, row locks and session isolation are unchanged. Cluster control
    happens outside this context.
    """
    import boto3
    import botocore.session
    import psycopg
    import selectors
    import socket
    from psycopg.conninfo import conninfo_to_dict

    original_connect = psycopg.connect

    class PollingSelector(selectors.DefaultSelector):
        def select(self, timeout=None):
            return super().select(0.01 if timeout is None else min(timeout, 0.01))

    def forbidden(*args, **kwargs):
        raise HarnessError("External AWS/network/process operation blocked")

    def connect(conninfo="", **kwargs):
        # DBAPI behavior options aren't libpq target options.
        parameters = conninfo_to_dict(conninfo, **{k: v for k, v in kwargs.items()
                                                  if k not in {"autocommit", "row_factory", "context", "prepare_threshold", "cursor_factory"}})
        cluster.validate_connection(parameters)
        return original_connect(conninfo, **kwargs)

    with patch.object(selectors, "DefaultSelector", PollingSelector), \
         patch.object(psycopg, "connect", connect), \
         patch.object(socket.socket, "connect", forbidden), \
         patch.object(socket.socket, "connect_ex", forbidden), \
         patch.object(socket.socket, "sendto", forbidden), \
         patch.object(subprocess, "Popen", forbidden), \
         patch.object(os, "system", forbidden), \
         patch.object(boto3, "client", forbidden), \
         patch.object(boto3, "resource", forbidden), \
         patch.object(boto3.session.Session, "client", forbidden), \
         patch.object(boto3.session.Session, "resource", forbidden), \
         patch.object(botocore.session.Session, "create_client", forbidden):
        yield
