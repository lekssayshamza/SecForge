#!/usr/bin/env python3
"""Backend host route manager + narrow root wrapper unit tests.

The wrapper is imported by path so no root install is required; real ``ip``
or ``sudo`` is never invoked. Route reads (``_ip_route_lines``) and writes
(``_run_wrapper``) are patched to an in-memory route simulator while the
desired-state logic runs against the same sqlite fixtures used across the
VPN authorization suite.
"""

import importlib.machinery
import importlib.util
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import backend_routes
from app.config import settings
from app.database import Base
from app.models import BuilderEnvironment, LabInstance, Room, User, VpnIdentity

WRAPPER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "deploy", "backend_routes"
)


def load_wrapper():
    loader = importlib.machinery.SourceFileLoader(
        "secforge_backend_routes_wrapper", WRAPPER_PATH
    )
    spec = importlib.util.spec_from_file_location(
        "secforge_backend_routes_wrapper", WRAPPER_PATH, loader=loader
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.fixture
def routes_enabled(monkeypatch):
    monkeypatch.setattr(settings, "vpn_enabled", True)
    monkeypatch.setattr(settings, "vpn_backend_authz_enabled", True)
    monkeypatch.setattr(settings, "terraform_dry_run", False)


@pytest.fixture
def route_sim(monkeypatch):
    """In-memory stand-in for /32 route state on the managed tunnel."""
    state = SimpleNamespace(current=set(), adds=[], dels=[])

    def fake_ip_route_lines():
        lines = []
        for address in sorted(state.current):
            lines.append(
                f"{address}/32 via {settings.backend_route_gateway} dev "
                f"{settings.backend_route_dev} proto static metric "
                f"{settings.backend_route_metric}"
            )
        return lines

    def fake_run_wrapper(action, address):
        if action == "add":
            state.adds.append(address)
            state.current.add(address)
        else:
            state.dels.append(address)
            state.current.discard(address)

    monkeypatch.setattr(backend_routes, "_ip_route_lines", fake_ip_route_lines)
    monkeypatch.setattr(backend_routes, "_run_wrapper", fake_run_wrapper)
    return state


def make_user(db, name: str, role: str = "learner") -> User:
    user = User(
        username=f"{name}_{uuid4().hex[:8]}",
        email=f"{name}_{uuid4().hex[:8]}@example.com",
        password_hash="hash",
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def make_room(db, creator: User) -> Room:
    room = Room(
        creator_id=creator.id,
        name=f"room_{uuid4().hex[:6]}",
        category="web",
        difficulty="beginner",
        status="published",
    )
    db.add(room)
    db.flush()
    return room


def make_backend(db) -> User:
    backend = make_user(db, "sf-backend", role="system")
    identity = VpnIdentity(
        user_id=backend.id,
        common_name="sf-backend",
        vpn_ip="10.8.0.67",
    )
    db.add(identity)
    db.flush()
    return backend


def make_builder(db, creator: User, room: Room, **overrides) -> BuilderEnvironment:
    fields = dict(
        room_id=room.id,
        creator_id=creator.id,
        status="running",
        private_ip="10.0.1.201",
        instance_id="i-0bld",
        security_group_id="sg-0bld",
        terraform_state_path="/tmp/test-builders/builder-test",
        expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    fields.update(overrides)
    builder = BuilderEnvironment(**fields)
    db.add(builder)
    db.flush()
    return builder


def make_lab(db, learner: User, room: Room, **overrides) -> LabInstance:
    fields = dict(
        room_id=room.id,
        learner_id=learner.id,
        status="running",
        private_ip="10.0.1.137",
        instance_id="i-0abc",
        security_group_id="sg-0abc",
        expires_at=datetime.utcnow() + timedelta(hours=2),
    )
    fields.update(overrides)
    lab = LabInstance(**fields)
    db.add(lab)
    db.flush()
    return lab


class TestDesiredBuilderIps:
    def test_disabled_flag_yields_nothing(self, db, routes_enabled, monkeypatch):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room)
        monkeypatch.setattr(settings, "vpn_backend_authz_enabled", False)
        assert backend_routes.desired_builder_ips(db) == set()

    def test_vpn_disabled_yields_nothing(self, db, routes_enabled, monkeypatch):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room)
        monkeypatch.setattr(settings, "vpn_enabled", False)
        assert backend_routes.desired_builder_ips(db) == set()

    def test_dry_run_yields_nothing(self, db, routes_enabled, monkeypatch):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room)
        monkeypatch.setattr(settings, "terraform_dry_run", True)
        assert backend_routes.desired_builder_ips(db) == set()

    def test_no_backend_identity_yields_nothing(self, db, routes_enabled):
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room)
        assert backend_routes.desired_builder_ips(db) == set()

    def test_running_builder_makes_desired_ip(self, db, routes_enabled):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.202")
        assert backend_routes.desired_builder_ips(db) == {"10.0.1.202"}

    @pytest.mark.parametrize(
        "status",
        ["pending", "provisioning", "stopping", "stopped", "failed"],
    )
    def test_non_running_builder_excluded(self, db, routes_enabled, status):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, status=status)
        assert backend_routes.desired_builder_ips(db) == set()

    def test_null_private_ip_excluded(self, db, routes_enabled):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip=None)
        assert backend_routes.desired_builder_ips(db) == set()

    def test_invalid_builder_ip_excluded(self, db, routes_enabled):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.999")
        assert backend_routes.desired_builder_ips(db) == set()

    def test_overlapping_running_lab_excluded(self, db, routes_enabled):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        learner = make_user(db, "learner")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.174")
        make_lab(db, learner, room, private_ip="10.0.1.174")
        assert backend_routes.desired_builder_ips(db) == set()

    def test_stopped_lab_does_not_block_builder(self, db, routes_enabled):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        learner = make_user(db, "learner")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.174")
        make_lab(db, learner, room, private_ip="10.0.1.174", status="stopped")
        assert backend_routes.desired_builder_ips(db) == {"10.0.1.174"}

    def test_multiple_running_builders(self, db, routes_enabled):
        make_backend(db)
        creator_a = make_user(db, "creator_a", role="creator")
        creator_b = make_user(db, "creator_b", role="creator")
        room_a = make_room(db, creator_a)
        room_b = make_room(db, creator_b)
        make_builder(db, creator_a, room_a, private_ip="10.0.1.210")
        make_builder(db, creator_b, room_b, private_ip="10.0.1.211")
        assert backend_routes.desired_builder_ips(db) == {
            "10.0.1.210",
            "10.0.1.211",
        }

    def test_lab_only_yields_nothing(self, db, routes_enabled):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        learner = make_user(db, "learner")
        room = make_room(db, creator)
        make_lab(db, learner, room)
        assert backend_routes.desired_builder_ips(db) == set()


class TestCurrentManagedIps:
    def test_parses_only_managed_routes(self, monkeypatch):
        lines = [
            "10.0.1.210/32 via 10.8.0.1 dev tun1 proto static metric 100",
            "10.0.1.211/32 via 10.8.0.1 dev tun1 metric 100",
            "10.0.1.212/32 via 10.8.0.1 dev tun0 metric 100",
            "10.0.1.137/32 dev tun1 scope link",
            "10.0.1.0/24 via 10.8.0.1 dev tun0 proto static metric 100",
            "10.0.1.213/32 via 10.8.0.2 dev tun1 metric 100",
            "10.0.1.214/32 via 10.8.0.1 dev tun1 metric 200",
            "10.0.1.215/32 via 10.8.0.1 dev tun1",
            "fd00::1/128 via fe80::1 dev tun1 metric 100",
        ]
        monkeypatch.setattr(backend_routes, "_ip_route_lines", lambda: lines)
        assert backend_routes.current_managed_ips() == {
            "10.0.1.210",
            "10.0.1.211",
        }

    def test_reads_whole_main_table_without_dev_filter(self, monkeypatch):
        """Regression: a ``dev`` filter strips the interface token from output."""
        captured = {}

        class _Proc:
            returncode = 0
            stdout = "10.0.1.174 via 10.8.0.1 dev tun1 metric 100\n"
            stderr = ""

        def fake_run(command, **kwargs):
            captured["command"] = command
            return _Proc()

        monkeypatch.setattr(backend_routes.subprocess, "run", fake_run)
        lines = backend_routes._ip_route_lines()
        assert captured["command"] == ["ip", "route", "show", "table", "main"]
        assert "dev" not in captured["command"]
        assert lines == ["10.0.1.174 via 10.8.0.1 dev tun1 metric 100"]

    def test_normal_output_with_dev_token_recognized(self):
        assert (
            backend_routes._managed_route_ip(
                "10.0.1.174 via 10.8.0.1 dev tun1 metric 100"
            )
            == "10.0.1.174"
        )

    def test_explicit_slash_32_host_route_recognized(self):
        assert (
            backend_routes._managed_route_ip(
                "10.0.1.174/32 via 10.8.0.1 dev tun1 metric 100"
            )
            == "10.0.1.174"
        )

    def test_learner_slash_24_route_not_recognized(self):
        assert (
            backend_routes._managed_route_ip(
                "10.0.1.0/24 via 10.8.0.1 dev tun1 metric 100"
            )
            is None
        )

    def test_filtered_output_without_dev_token_not_recognized(self):
        """Ambiguous host routes lacking an interface are never treated as ours."""
        assert (
            backend_routes._managed_route_ip(
                "10.0.1.174 via 10.8.0.1 metric 100"
            )
            is None
        )

    def test_read_failure_raises(self, monkeypatch):
        proc = MagicMock()
        proc.returncode = 2
        proc.stderr = "Cannot find device tun1"
        monkeypatch.setattr(
            backend_routes,
            "subprocess",
            SimpleNamespace(run=lambda *a, **k: proc),
        )
        with pytest.raises(backend_routes.BackendRouteError):
            backend_routes.current_managed_ips()


class TestSyncBackendRoutes:
    def test_adds_missing_route(self, db, routes_enabled, route_sim):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.210")
        added, removed = backend_routes.sync_backend_routes(db)
        assert added == {"10.0.1.210"}
        assert removed == set()
        assert route_sim.current == {"10.0.1.210"}

    def test_idempotent_when_in_sync(self, db, routes_enabled, route_sim):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.210")
        route_sim.current.add("10.0.1.210")
        added, removed = backend_routes.sync_backend_routes(db)
        assert added == set()
        assert removed == set()
        assert route_sim.current == {"10.0.1.210"}
        # An already-correct route must not invoke the wrapper at all.
        assert route_sim.adds == []
        assert route_sim.dels == []

    def test_prunes_stale_route(self, db, routes_enabled, route_sim):
        make_backend(db)
        route_sim.current.add("10.0.1.250")
        added, removed = backend_routes.sync_backend_routes(db)
        assert added == set()
        assert removed == {"10.0.1.250"}
        assert route_sim.current == set()

    def test_flag_off_prunes_managed_routes(
        self, db, routes_enabled, route_sim, monkeypatch
    ):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.210")
        route_sim.current.update({"10.0.1.210", "10.0.1.250"})
        monkeypatch.setattr(settings, "vpn_backend_authz_enabled", False)
        backend_routes.sync_backend_routes(db)
        assert route_sim.current == set()

    def test_stop_prunes_when_lab_overlaps(self, db, routes_enabled, route_sim):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        learner = make_user(db, "learner")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.174")
        route_sim.current.update({"10.0.1.174"})
        make_lab(db, learner, room, private_ip="10.0.1.174", status="running")
        backend_routes.sync_backend_routes(db)
        assert route_sim.current == set()

    def test_removal_precedes_addition(self, db, routes_enabled, route_sim):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.210")
        route_sim.current.update({"10.0.1.210", "10.0.1.211"})
        added, removed = backend_routes.sync_backend_routes(db)
        assert removed == {"10.0.1.211"}
        assert added == set()
        assert route_sim.current == {"10.0.1.210"}

    def test_unrelated_and_learner_routes_never_removed(
        self, db, routes_enabled, monkeypatch
    ):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.174")
        lines = [
            "default via 192.168.1.1 dev wlan0 proto dhcp metric 600",
            "10.0.1.0/24 via 10.8.0.1 dev tun0 proto static metric 100",
            "10.0.1.0/24 via 10.8.0.1 dev tun0 proto static metric 200",
            "10.0.1.123/32 via 10.8.0.1 dev tun0 metric 50",
            "10.0.1.174/32 via 10.8.0.1 dev tun1 proto static metric 100",
            "10.0.1.250/32 via 10.8.0.1 dev tun1 proto static metric 100",
            "10.8.0.0/24 dev tun0 proto kernel scope link src 10.8.0.65",
            "10.8.0.0/24 dev tun1 proto kernel scope link src 10.8.0.67",
        ]
        monkeypatch.setattr(backend_routes, "_ip_route_lines", lambda: lines)
        ops = []
        monkeypatch.setattr(
            backend_routes,
            "_run_wrapper",
            lambda action, address: ops.append((action, address)),
        )
        added, removed = backend_routes.sync_backend_routes(db)
        assert added == set()
        assert removed == {"10.0.1.250"}
        # Only the stale managed route is touched; the learner /24, the
        # unrelated /32 on tun0 and the link/default routes are untouched.
        assert ops == [("del", "10.0.1.250")]

    def test_existing_route_in_real_output_is_a_noop(
        self, db, routes_enabled, monkeypatch
    ):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.174")
        lines = [
            "default via 192.168.1.1 dev wlan0 proto dhcp metric 600",
            "10.0.1.0/24 via 10.8.0.1 dev tun0 proto static metric 100",
            "10.0.1.174/32 via 10.8.0.1 dev tun1 proto static metric 100",
            "10.8.0.0/24 dev tun1 proto kernel scope link src 10.8.0.67",
        ]
        monkeypatch.setattr(backend_routes, "_ip_route_lines", lambda: lines)
        ops = []
        monkeypatch.setattr(
            backend_routes,
            "_run_wrapper",
            lambda action, address: ops.append((action, address)),
        )
        added, removed = backend_routes.sync_backend_routes(db)
        assert added == set()
        assert removed == set()
        assert ops == []

    def test_wrapper_failure_raises(self, db, routes_enabled, monkeypatch):
        make_backend(db)
        creator = make_user(db, "creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.210")
        monkeypatch.setattr(backend_routes, "_ip_route_lines", lambda: [])

        def boom(action, address):
            raise backend_routes.BackendRouteError("sudo unavailable")

        monkeypatch.setattr(backend_routes, "_run_wrapper", boom)
        with pytest.raises(backend_routes.BackendRouteError):
            backend_routes.sync_backend_routes(db)

    def test_dry_run_shortcircuits_without_touching_routes(
        self, db, route_sim, monkeypatch
    ):
        route_sim.current.add("10.0.1.250")
        monkeypatch.setattr(
            backend_routes, "_route_sync_allowed", lambda: False
        )
        result = backend_routes.sync_backend_routes(db)
        assert result == (set(), set())
        assert route_sim.current == {"10.0.1.250"}

    def test_lock_contention_raises(self, db, routes_enabled, monkeypatch):
        backend_routes._ROUTE_LOCK.acquire()
        try:
            monkeypatch.setattr(backend_routes, "current_managed_ips", lambda: set())
            monkeypatch.setattr(backend_routes, "desired_builder_ips", lambda db: set())
            with pytest.raises(backend_routes.BackendRouteError):
                backend_routes.sync_backend_routes(db)
        finally:
            backend_routes._ROUTE_LOCK.release()


class TestRouteWrapper:
    def test_loadable(self):
        wrapper = load_wrapper()
        assert callable(wrapper.validate_args)

    def test_valid_add(self):
        wrapper = load_wrapper()
        assert wrapper.validate_args(
            ["secforge_backend_routes", "add", "10.0.1.174"]
        ) == ("add", "10.0.1.174")

    def test_valid_del(self):
        wrapper = load_wrapper()
        assert wrapper.validate_args(
            ["secforge_backend_routes", "del", "10.0.1.174"]
        ) == ("del", "10.0.1.174")

    @pytest.mark.parametrize(
        "argv",
        [
            ["backend_routes"],
            ["backend_routes", "add"],
            ["backend_routes", "add", "10.0.1.174", "extra"],
            ["backend_routes", "modify", "10.0.1.174"],
            ["backend_routes", "add", "10.0.1.174; rm -rf /"],
            ["backend_routes", "add", "10.0.1"],
            ["backend_routes", "add", "10.0.1.256"],
            ["backend_routes", "add", "1.2.3.4.5"],
            ["backend_routes", "add", "dead:beef::1"],
            ["backend_routes", "add", "$(reboot)"],
        ],
    )
    def test_rejects_invalid(self, argv):
        wrapper = load_wrapper()
        with pytest.raises(wrapper.WrapperError):
            wrapper.validate_args(argv)

    def test_main_add_calls_replace(self):
        wrapper = load_wrapper()
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        with patch.object(wrapper.subprocess, "run", return_value=proc) as run:
            rc = wrapper.main(["backend_routes", "add", "10.0.1.174"])
        assert rc == 0
        command = run.call_args.args[0]
        assert command == [
            "ip",
            "route",
            "replace",
            "10.0.1.174/32",
            "via",
            "10.8.0.1",
            "dev",
            "tun1",
            "metric",
            "100",
        ]

    def test_main_del_tolerates_no_such_process(self):
        wrapper = load_wrapper()
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "RTNETLINK answers: No such process"
        with patch.object(wrapper.subprocess, "run", return_value=proc) as run:
            rc = wrapper.main(["backend_routes", "del", "10.0.1.174"])
        assert rc == 0
        assert run.call_args.args[0][3] == "10.0.1.174/32"

    def test_main_del_failure_propagates(self):
        wrapper = load_wrapper()
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "RTNETLINK answers: Network is unreachable"
        with patch.object(wrapper.subprocess, "run", return_value=proc):
            rc = wrapper.main(["backend_routes", "del", "10.0.1.174"])
        assert rc == 1

    def test_main_usage_error_returns_two(self):
        wrapper = load_wrapper()
        assert wrapper.main(["backend_routes"]) == 2