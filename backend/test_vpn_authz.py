#!/usr/bin/env python3
"""Tests for dynamic learner-to-lab VPN authorization (Phase 3).

Gateway operations are mocked throughout; no live network calls are made.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import ipaddress
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app import lab_manager as lab_manager_module
from app import reconciliation as reconciliation_module
from app import vpn_authz, vpn_routes
from app.config import Settings, settings
from app.database import Base, SessionLocal
from app.lab_manager import LabManager
from app.main import app
from app.models import BuilderEnvironment, LabInstance, MachineTemplate, Room, RoomTemplate, User, VpnIdentity
from app.ops_lock import OperationInProgress
from app.reconciliation import ReconciliationWorker
from app.security import create_access_token
from app.vpn import (
    EXCLUDED_VPN_IPS,
    VpnManager,
    VpnManagerError,
    validate_vpn_ip,
)
from app.vpn_authz import (
    VpnAuthorizationError,
    authorize_builder_access,
    authorize_lab_access,
    desired_pairs,
    find_filter_rule_handle,
    render_nft_transaction,
    sync_vpn_authz,
    validate_lab_ip,
)

client = TestClient(app)

CA_PEM = "-----BEGIN CERTIFICATE-----\nQ0EtRklYVFVSRQo=\n-----END CERTIFICATE-----\n"
CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\nTEVBTkVSLUNFUlQtRklYVFVSRQo=\n"
    "-----END CERTIFICATE-----\n"
)
KEY_PEM = (
    "-----BEGIN PRIVATE KEY-----\nTEVBTkVSLUtFWS1GSVhUVVJFCg==\n"
    "-----END PRIVATE KEY-----\n"
)
TA_PEM = (
    "#\n# 2048 bit OpenVPN static key\n#\n"
    "-----BEGIN OpenVPN Static key V1-----\nVEEtRklYVFVSRQo=\n"
    "-----END OpenVPN Static key V1-----\n"
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


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "test_id.pem"
    path.write_text("placeholder-not-a-real-key\n")
    return str(path)


@pytest.fixture
def enabled_vpn(monkeypatch, key_file):
    monkeypatch.setattr(settings, "vpn_enabled", True)
    monkeypatch.setattr(settings, "vpn_authz_enabled", True)
    monkeypatch.setattr(settings, "vpn_gateway_ssh_key_path", key_file)


@pytest.fixture
def authz_on(enabled_vpn, monkeypatch):
    monkeypatch.setattr(settings, "terraform_dry_run", False)


@pytest.fixture
def backend_authz_on(authz_on, monkeypatch):
    monkeypatch.setattr(settings, "vpn_backend_authz_enabled", True)


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
    template = RoomTemplate(room_id=room.id, version=1, created_by=creator.id)
    db.add(template)
    db.flush()
    db.add(MachineTemplate(room_template_id=template.id, status="available", ami_id="ami-offline-challenge"))
    db.flush()
    return room


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


def make_identity(db, user: User, vpn_ip: str) -> VpnIdentity:
    identity = VpnIdentity(
        user_id=user.id,
        common_name=f"sf-{user.id}",
        vpn_ip=vpn_ip,
    )
    db.add(identity)
    db.flush()
    return identity


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


def make_backend(db) -> VpnIdentity:
    user = User(
        username="sf-backend",
        email="sf-backend@example.com",
        password_hash="hash",
        role="system",
    )
    db.add(user)
    db.flush()
    identity = VpnIdentity(
        user_id=user.id,
        common_name="sf-backend",
        vpn_ip="10.8.0.67",
    )
    db.add(identity)
    db.flush()
    return identity


@pytest.fixture
def lab_env(db):
    creator = make_user(db, "authz_creator", role="creator")
    room = make_room(db, creator)
    learner_a = make_user(db, "authz_learner_a")
    learner_b = make_user(db, "authz_learner_b")
    return SimpleNamespace(
        creator=creator,
        room=room,
        learner_a=learner_a,
        learner_b=learner_b,
    )


@pytest.fixture
def dry_lab_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "terraform_dry_run", True)
    monkeypatch.setattr(
        settings, "lab_terraform_workspace_root", str(tmp_path / "labs")
    )
    monkeypatch.setattr(
        settings, "terraform_plugin_cache_dir", str(tmp_path / "plugins")
    )
    return LabManager()


@pytest.fixture
def recording_vpn_manager(monkeypatch):
    applied: list[set[tuple[str, str]]] = []
    lock = threading.Lock()

    def _list_forward_chain_rules(self):
        return ""

    def _sync_forward_rules(self, pairs, existing_rule_handle=None):
        with lock:
            applied.append(set(pairs))

    manager_cls = VpnManager
    original_sync = manager_cls.sync_forward_rules
    original_list = manager_cls.list_forward_chain_rules
    monkeypatch.setattr(manager_cls, "sync_forward_rules", _sync_forward_rules)
    monkeypatch.setattr(
        manager_cls, "list_forward_chain_rules", _list_forward_chain_rules
    )
    monkeypatch.setattr(settings, "vpn_enabled", True)
    monkeypatch.setattr(settings, "vpn_authz_enabled", True)
    monkeypatch.setattr(settings, "terraform_dry_run", False)
    yield applied
    monkeypatch.setattr(manager_cls, "sync_forward_rules", original_sync)
    monkeypatch.setattr(manager_cls, "list_forward_chain_rules", original_list)


def make_authz_manager() -> MagicMock:
    manager = MagicMock(spec=VpnManager)
    manager.list_forward_chain_rules.return_value = ""
    return manager


class TestConfiguration:
    def test_lab_private_cidr_default(self):
        assert Settings.model_fields["lab_private_cidr"].default == "10.0.1.0/24"

    def test_vpn_authz_enabled_default(self):
        assert Settings.model_fields["vpn_authz_enabled"].default is True

    def test_vpn_backend_authz_enabled_default_off(self):
        assert Settings.model_fields["vpn_backend_authz_enabled"].default is False

    def test_lab_cidr_disjoint_from_learner_range(self):
        lab_net = ipaddress.ip_network(settings.lab_private_cidr)
        learner_net = ipaddress.ip_network(settings.vpn_learner_ip_range)
        assert not lab_net.overlaps(learner_net)

    def test_excluded_test_client_unchanged(self):
        assert "10.8.0.20" in EXCLUDED_VPN_IPS


class TestValidateLabIp:
    def test_valid_address(self):
        assert validate_lab_ip("10.0.1.137") == "10.0.1.137"

    def test_network_address_rejected(self):
        with pytest.raises(VpnAuthorizationError):
            validate_lab_ip("10.0.1.0")

    def test_broadcast_address_rejected(self):
        with pytest.raises(VpnAuthorizationError):
            validate_lab_ip("10.0.1.255")

    def test_vpn_range_address_rejected(self):
        with pytest.raises(VpnAuthorizationError):
            validate_lab_ip("10.8.0.65")

    def test_other_subnet_rejected(self):
        with pytest.raises(VpnAuthorizationError):
            validate_lab_ip("10.0.2.5")

    def test_malformed_rejected(self):
        for bad in ("not-an-ip", "10.0.1.", "10.0.1", "10.0.1.137.5", ""):
            with pytest.raises(VpnAuthorizationError):
                validate_lab_ip(bad)

    def test_ipv6_rejected(self):
        for bad in ("::1", "fe80::1", "2001:db8::1"):
            with pytest.raises(VpnAuthorizationError):
                validate_lab_ip(bad)

    def test_nft_metacharacters_rejected(self):
        for bad in (
            "10.0.1.137; delete table inet filter",
            "10.0.1.137\ndelete ruleset",
            "10.0.1.137 } chain evil {",
            "10.0.1.137) accept #",
            "$(id)",
            "`id`",
            "10.0.1.137\0",
        ):
            with pytest.raises(VpnAuthorizationError):
                validate_lab_ip(bad)

    def test_non_string_rejected(self):
        for bad in (None, 123, 1.5, ["10.0.1.1"]):
            with pytest.raises(VpnAuthorizationError):
                validate_lab_ip(bad)

    def test_surrounding_whitespace_rejected(self):
        with pytest.raises(VpnAuthorizationError):
            validate_lab_ip(" 10.0.1.137")
        with pytest.raises(VpnAuthorizationError):
            validate_lab_ip("10.0.1.137 ")


class TestDesiredPairs:
    def test_running_lab_with_identity_yields_pair(self, db, lab_env):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        assert desired_pairs(db) == {("10.8.0.65", "10.0.1.137")}

    def test_cross_learner_isolation(self, db, lab_env):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_identity(db, lab_env.learner_b, "10.8.0.66")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        make_lab(db, lab_env.learner_b, lab_env.room, private_ip="10.0.1.140")
        pairs = desired_pairs(db)
        assert pairs == {
            ("10.8.0.65", "10.0.1.137"),
            ("10.8.0.66", "10.0.1.140"),
        }
        assert ("10.8.0.65", "10.0.1.140") not in pairs
        assert ("10.8.0.66", "10.0.1.137") not in pairs

    @pytest.mark.parametrize(
        "status",
        ["pending", "provisioning", "stopping", "stopped", "failed"],
    )
    def test_non_running_status_yields_no_pair(self, db, lab_env, status):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, status=status)
        assert desired_pairs(db) == set()

    def test_running_without_private_ip_yields_no_pair(self, db, lab_env):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip=None)
        assert desired_pairs(db) == set()

    def test_running_lab_without_identity_yields_no_pair(self, db, lab_env):
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        assert desired_pairs(db) == set()

    def test_revoked_identity_yields_no_pair(self, db, lab_env):
        identity = make_identity(db, lab_env.learner_a, "10.8.0.65")
        identity.revoked_at = datetime.utcnow()
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        assert desired_pairs(db) == set()

    def test_invalid_identity_address_row_is_skipped(self, db, lab_env):
        make_identity(db, lab_env.learner_a, "not-an-ip")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        assert desired_pairs(db) == set()

    def test_poisoned_row_does_not_break_other_pairs(self, db, lab_env):
        make_identity(db, lab_env.learner_a, "10.8.0.65; delete ruleset")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        make_identity(db, lab_env.learner_b, "10.8.0.66")
        make_lab(db, lab_env.learner_b, lab_env.room, private_ip="10.0.1.140")
        assert desired_pairs(db) == {("10.8.0.66", "10.0.1.140")}

    def test_poisoned_lab_ip_row_is_skipped(self, db, lab_env):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            private_ip="10.0.1.137 } chain evil {",
        )
        make_identity(db, lab_env.learner_b, "10.8.0.66")
        make_lab(db, lab_env.learner_b, lab_env.room, private_ip="10.0.1.140")
        assert desired_pairs(db) == {("10.8.0.66", "10.0.1.140")}


class TestBuilderDesiredPairs:
    def test_running_builder_with_identity_yields_pair(self, db, lab_env):
        make_identity(db, lab_env.creator, "10.8.0.70")
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        assert desired_pairs(db) == {("10.8.0.70", "10.0.1.201")}

    def test_builder_authorization_is_creator_scoped(self, db, lab_env):
        other_creator = make_user(db, "authz_other_creator", role="creator")
        other_room = make_room(db, other_creator)
        make_identity(db, lab_env.creator, "10.8.0.70")
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        make_identity(db, other_creator, "10.8.0.71")
        make_builder(db, other_creator, other_room, private_ip="10.0.1.202")
        assert desired_pairs(db) == {
            ("10.8.0.70", "10.0.1.201"),
            ("10.8.0.71", "10.0.1.202"),
        }

    def test_learner_identity_never_authorizes_builder(self, db, lab_env):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        assert ("10.8.0.65", "10.0.1.201") not in desired_pairs(db)

    @pytest.mark.parametrize(
        "status", ["pending", "provisioning", "stopping", "stopped", "failed"]
    )
    def test_non_running_builder_yields_no_pair(self, db, lab_env, status):
        make_identity(db, lab_env.creator, "10.8.0.70")
        make_builder(
            db, lab_env.creator, lab_env.room, status=status, private_ip="10.0.1.201"
        )
        assert desired_pairs(db) == set()

    def test_running_builder_without_private_ip_yields_no_pair(self, db, lab_env):
        make_identity(db, lab_env.creator, "10.8.0.70")
        make_builder(db, lab_env.creator, lab_env.room, private_ip=None)
        assert desired_pairs(db) == set()

    def test_running_builder_without_identity_yields_no_pair(self, db, lab_env):
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        assert desired_pairs(db) == set()

    def test_revoked_creator_identity_yields_no_pair(self, db, lab_env):
        identity = make_identity(db, lab_env.creator, "10.8.0.70")
        identity.revoked_at = datetime.utcnow()
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        assert desired_pairs(db) == set()

    def test_builder_and_lab_pairs_coexist(self, db, lab_env):
        make_identity(db, lab_env.creator, "10.8.0.70")
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        assert desired_pairs(db) == {
            ("10.8.0.70", "10.0.1.201"),
            ("10.8.0.65", "10.0.1.137"),
        }

    def test_replaced_builder_authorizes_only_new_ip(self, db, lab_env):
        make_identity(db, lab_env.creator, "10.8.0.70")
        old = make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        db.delete(old)
        db.flush()
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.205")
        assert desired_pairs(db) == {("10.8.0.70", "10.0.1.205")}

    def test_invalid_builder_ip_row_is_skipped(self, db, lab_env):
        make_identity(db, lab_env.creator, "10.8.0.70")
        make_builder(db, lab_env.creator, lab_env.room, private_ip="not-an-ip")
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        assert desired_pairs(db) == {("10.8.0.65", "10.0.1.137")}


class TestBackendDesiredPairs:
    def test_disabled_flag_yields_no_backend_pair(self, db, monkeypatch):
        monkeypatch.setattr(settings, "vpn_backend_authz_enabled", False)
        make_backend(db)
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.201")
        assert desired_pairs(db) == set()

    def test_running_builder_yields_backend_pair(self, db, backend_authz_on):
        make_backend(db)
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.201")
        assert desired_pairs(db) == {("10.8.0.67", "10.0.1.201")}

    @pytest.mark.parametrize(
        "status", ["pending", "provisioning", "stopping", "stopped", "failed"]
    )
    def test_non_running_builder_yields_no_pair(self, db, backend_authz_on, status):
        make_backend(db)
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(
            db, creator, room, status=status, private_ip="10.0.1.201"
        )
        assert desired_pairs(db) == set()

    def test_running_builder_without_private_ip_yields_no_pair(
        self, db, backend_authz_on
    ):
        make_backend(db)
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip=None)
        assert desired_pairs(db) == set()

    def test_multiple_running_builders_yield_multiple_pairs(
        self, db, backend_authz_on
    ):
        make_backend(db)
        for index in range(2):
            creator = make_user(db, f"bk_c_{index}", role="creator")
            room = make_room(db, creator)
            make_builder(db, creator, room, private_ip=f"10.0.1.{201 + index}")
        assert desired_pairs(db) == {
            ("10.8.0.67", "10.0.1.201"),
            ("10.8.0.67", "10.0.1.202"),
        }

    def test_missing_backend_identity_no_pair_no_exception(
        self, db, backend_authz_on
    ):
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.201")
        assert desired_pairs(db) == set()

    def test_backend_identity_without_system_role_not_used(
        self, db, backend_authz_on
    ):
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        user = make_user(db, "sf-backend_user", role="learner")
        identity = VpnIdentity(
            user_id=user.id, common_name="sf-backend", vpn_ip="10.8.0.67"
        )
        db.add(identity)
        db.flush()
        make_builder(db, creator, room, private_ip="10.0.1.201")
        assert desired_pairs(db) == set()

    def test_revoked_backend_identity_yields_no_pair(self, db, backend_authz_on):
        identity = make_backend(db)
        identity.revoked_at = datetime.utcnow()
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.201")
        assert desired_pairs(db) == set()

    def test_invalid_backend_address_yields_no_pair(self, db, backend_authz_on):
        make_backend(db)
        identity = db.scalar(select(VpnIdentity).where(VpnIdentity.common_name == "sf-backend"))
        identity.vpn_ip = "not-an-ip"
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.201")
        assert desired_pairs(db) == set()

    def test_invalid_builder_ip_skipped(self, db, backend_authz_on):
        make_backend(db)
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="not-an-ip")
        assert desired_pairs(db) == set()

    def test_invalid_builder_ip_skipped_other_pairs_kept(
        self, db, backend_authz_on, lab_env
    ):
        make_backend(db)
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="not-an-ip")
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        assert desired_pairs(db) == {("10.8.0.65", "10.0.1.137")}

    def test_learner_authorization_unchanged(self, db, backend_authz_on, lab_env):
        make_backend(db)
        learner = make_lab(db, lab_env.learner_a, lab_env.room)
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        assert desired_pairs(db) == {("10.8.0.65", learner.private_ip)}

    def test_creator_authorization_unchanged(self, db, backend_authz_on, lab_env):
        make_backend(db)
        make_identity(db, lab_env.creator, "10.8.0.70")
        make_builder(db, lab_env.creator, lab_env.room, private_ip="10.0.1.201")
        assert desired_pairs(db) == {
            ("10.8.0.70", "10.0.1.201"),
            ("10.8.0.67", "10.0.1.201"),
        }

    def test_backend_identity_not_authorized_to_labs(
        self, db, backend_authz_on, lab_env
    ):
        make_backend(db)
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        pairs = desired_pairs(db)
        assert pairs == {("10.8.0.65", "10.0.1.137")}
        assert ("10.8.0.67", "10.0.1.137") not in pairs

    def test_duplicate_pairs_deduplicated(self, db, backend_authz_on):
        make_backend(db)
        for index in range(2):
            creator = make_user(db, f"bk_dup_{index}", role="creator")
            room = make_room(db, creator)
            make_builder(db, creator, room, private_ip="10.0.1.201")
        assert desired_pairs(db) == {("10.8.0.67", "10.0.1.201")}

    def test_no_running_builders_fail_closed(self, db, backend_authz_on):
        make_backend(db)
        assert desired_pairs(db) == set()

    def test_sync_applies_backend_pair(self, db, backend_authz_on):
        make_backend(db)
        creator = make_user(db, "bk_creator", role="creator")
        room = make_room(db, creator)
        make_builder(db, creator, room, private_ip="10.0.1.201")
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = sync_vpn_authz(db)
        assert applied == {("10.8.0.67", "10.0.1.201")}
        manager.sync_forward_rules.assert_called_once_with(
            {("10.8.0.67", "10.0.1.201")}, existing_rule_handle=None
        )


class TestRenderNftTransaction:
    def test_single_pair_structure(self):
        tx = render_nft_transaction({("10.8.0.65", "10.0.1.137")})
        assert "destroy table inet secforge-vpn" in tx
        assert "table inet secforge-vpn {" in tx
        assert "set lab_pairs {" in tx
        assert "type ipv4_addr . ipv4_addr" in tx
        assert "elements = {" in tx
        assert "10.8.0.65 . 10.0.1.137" in tx
        assert (
            'add rule inet filter forward iifname "tun0" ip saddr . ip daddr '
            "{ 10.8.0.65 . 10.0.1.137 } "
            'accept comment "secforge-vpn-authz"'
        ) in tx
        assert "chain forward {" not in tx
        assert "hook forward" not in tx
        assert tx.endswith("\n")

    def test_rule_embeds_same_elements_as_set(self):
        pairs = {("10.8.0.65", "10.0.1.137"), ("10.8.0.66", "10.0.1.140")}
        tx = render_nft_transaction(pairs)
        for learner_ip, lab_ip in pairs:
            assert tx.count(f"{learner_ip} . {lab_ip}") == 2
        assert "@secforge-vpn" not in tx

    def test_delete_before_destroy_before_add_with_handle(self):
        tx = render_nft_transaction(
            {("10.8.0.65", "10.0.1.137")}, existing_rule_handle=7
        )
        delete_index = tx.index("delete rule inet filter forward handle 7")
        destroy_index = tx.index("destroy table inet secforge-vpn")
        add_index = tx.index("add rule inet filter forward")
        assert delete_index < destroy_index < add_index

    def test_no_delete_line_without_handle(self):
        tx = render_nft_transaction({("10.8.0.65", "10.0.1.137")})
        assert "delete rule" not in tx

    @pytest.mark.parametrize("handle", [0, -1, True, False, "7", 7.5, [7]])
    def test_invalid_handle_rejected(self, handle):
        with pytest.raises(VpnAuthorizationError):
            render_nft_transaction(set(), existing_rule_handle=handle)

    def test_multiple_pairs_sorted_and_deterministic(self):
        pairs = {
            ("10.8.0.66", "10.0.1.140"),
            ("10.8.0.65", "10.0.1.137"),
            ("10.8.0.70", "10.0.1.100"),
        }
        tx = render_nft_transaction(pairs)
        first = tx.index("10.8.0.65 . 10.0.1.137")
        second = tx.index("10.8.0.66 . 10.0.1.140")
        third = tx.index("10.8.0.70 . 10.0.1.100")
        assert first < second < third

    def test_order_independence(self):
        forward = render_nft_transaction(
            [("10.8.0.65", "10.0.1.137"), ("10.8.0.66", "10.0.1.140")]
        )
        reverse = render_nft_transaction(
            [("10.8.0.66", "10.0.1.140"), ("10.8.0.65", "10.0.1.137")]
        )
        assert forward == reverse

    def test_duplicates_collapse(self):
        tx = render_nft_transaction(
            [
                ("10.8.0.65", "10.0.1.137"),
                ("10.8.0.65", "10.0.1.137"),
            ]
        )
        assert tx.count("10.8.0.65 . 10.0.1.137") == 2

    def test_idempotent_rendering(self):
        pairs = {("10.8.0.65", "10.0.1.137")}
        assert render_nft_transaction(pairs) == render_nft_transaction(pairs)

    def test_empty_pairs_valid_table_without_elements(self):
        tx = render_nft_transaction(set())
        assert "destroy table inet secforge-vpn" in tx
        assert "table inet secforge-vpn {" in tx
        assert "set lab_pairs {" in tx
        assert "elements =" not in tx

    def test_empty_pairs_omits_rule_fail_closed(self):
        tx = render_nft_transaction(set())
        assert "add rule" not in tx
        assert "delete rule" not in tx
        assert "destroy table inet secforge-vpn" in tx
        assert "set lab_pairs {" in tx

    def test_empty_pairs_still_deletes_existing_rule(self):
        tx = render_nft_transaction(set(), existing_rule_handle=9)
        assert "delete rule inet filter forward handle 9" in tx
        assert "add rule" not in tx

    def test_forbidden_content_absent(self):
        tx = render_nft_transaction({("10.8.0.65", "10.0.1.137")})
        for forbidden in (
            "flush ruleset",
            "flush chain",
            "flush table",
            "delete ruleset",
            "table inet filter {",
            "chain input",
            "chain output",
            "masquerade",
            "snat",
            "dnat",
            "redir",
            "udp",
            "dport",
            "redirect-gateway",
            "10.8.0.0/24",
            "10.0.1.0/24",
            "/etc/openvpn",
            "established",
            "policy drop",
            "hook forward",
            "@secforge-vpn",
        ):
            assert forbidden not in tx

    def test_no_nat_in_empty_transaction(self):
        tx = render_nft_transaction(set())
        for forbidden in ("masquerade", "snat", "dnat", "nat "):
            assert forbidden not in tx

    @pytest.mark.parametrize(
        "pair",
        [
            ("10.8.0.65; destroy table inet filter", "10.0.1.137"),
            ("10.8.0.65", "10.0.1.137; destroy table inet filter"),
            ("10.8.0.65", "10.0.1.137\ndestroy table inet filter"),
            ("10.8.0.65\n", "10.0.1.137"),
            ("10.8.0.65", "10.0.1.137 }"),
            ("$(id)", "10.0.1.137"),
            ("10.8.0.65", "`id`"),
            ("10.8.0.20", "10.0.1.137"),
            ("10.9.0.1", "10.0.1.137"),
            ("10.8.0.65", "10.8.0.65"),
            ("10.8.0.65", "192.168.1.1"),
            ("10.8.0.65", "::1"),
            ("::1", "10.0.1.137"),
            ("10.8.0.65", None),
            (None, "10.0.1.137"),
        ],
    )
    def test_injection_and_invalid_pairs_rejected(self, pair):
        with pytest.raises(VpnAuthorizationError):
            render_nft_transaction({pair})

    def test_malformed_pair_structure_rejected(self):
        for bad in (
            ["only-one"],
            ("a", "b", "c"),
            "10.8.0.65.10.0.1.137",
            {"learner": "10.8.0.65"},
            42,
        ):
            with pytest.raises(VpnAuthorizationError):
                render_nft_transaction([bad])


class TestFindFilterRuleHandle:
    LISTING_WITH_RULE = (
        "table inet filter {\n"
        "\tchain forward {\n"
        "\t\ttype filter hook forward priority filter; policy drop;\n"
        "\t\tct state established,related accept # handle 10\n"
        '\t\tiifname "tun0" ip saddr . ip daddr @secforge-vpn.lab_pairs '
        'accept comment "secforge-vpn-authz" # handle 12\n'
        "\t}\n"
        "}\n"
    )
    LISTING_WITHOUT_RULE = (
        "table inet filter {\n"
        "\tchain forward {\n"
        "\t\ttype filter hook forward priority filter; policy drop;\n"
        "\t\tct state established,related accept # handle 10\n"
        "\t}\n"
        "}\n"
    )

    def test_returns_handle_of_marked_rule(self):
        assert find_filter_rule_handle(self.LISTING_WITH_RULE) == 12

    def test_returns_none_when_rule_absent(self):
        assert find_filter_rule_handle(self.LISTING_WITHOUT_RULE) is None

    def test_ignores_unmarked_rules(self):
        listing = (
            self.LISTING_WITHOUT_RULE.replace(
                "ct state established,related accept # handle 10",
                'iifname "tun0" ip saddr . ip daddr @secforge-vpn.lab_pairs '
                'accept # handle 11',
            )
        )
        assert find_filter_rule_handle(listing) is None

    def test_non_string_listing_rejected(self):
        for bad in (None, 42, b"bytes", object()):
            with pytest.raises(VpnAuthorizationError):
                find_filter_rule_handle(bad)

    def test_marked_line_without_handle_rejected(self):
        listing = (
            '\t\tiifname "tun0" ip saddr . ip daddr @secforge-vpn.lab_pairs '
            'accept comment "secforge-vpn-authz"\n'
        )
        with pytest.raises(VpnAuthorizationError):
            find_filter_rule_handle(listing)

    def test_zero_handle_rejected(self):
        listing = (
            '\t\tiifname "tun0" ip saddr . ip daddr @secforge-vpn.lab_pairs '
            'accept comment "secforge-vpn-authz" # handle 0\n'
        )
        with pytest.raises(VpnAuthorizationError):
            find_filter_rule_handle(listing)

    def test_duplicate_marked_rules_rejected(self):
        line = (
            '\t\tiifname "tun0" ip saddr . ip daddr @secforge-vpn.lab_pairs '
            'accept comment "secforge-vpn-authz" # handle {}\n'
        )
        listing = line.format(12) + line.format(13)
        with pytest.raises(VpnAuthorizationError):
            find_filter_rule_handle(listing)


class TestVpnManagerSyncForwardRules:
    def test_uses_nft_f_with_stdin_transaction(self, enabled_vpn):
        captured = {}

        def fake_run_command(self, remote_args, timeout=None, *, stdin_data=None):
            captured["args"] = list(remote_args)
            captured["stdin"] = stdin_data
            from app.vpn import SshResult

            return SshResult(stdout="", stderr="", returncode=0)

        with patch.object(VpnManager, "run_command", fake_run_command):
            VpnManager().sync_forward_rules({("10.8.0.65", "10.0.1.137")})

        assert captured["args"] == ["sudo", "-n", "nft", "-f", "-"]
        assert isinstance(captured["stdin"], str)
        assert "table inet secforge-vpn" in captured["stdin"]
        assert "10.8.0.65 . 10.0.1.137" in captured["stdin"]
        assert "table inet filter" not in captured["stdin"]

    def test_no_shell_and_stdin_used(self, enabled_vpn):
        captured = {}

        def fake_subprocess_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        with patch("app.vpn.subprocess.run", fake_subprocess_run):
            VpnManager().sync_forward_rules(set())

        assert isinstance(captured["argv"], list)
        assert all(isinstance(token, str) for token in captured["argv"])
        assert "shell" not in captured["kwargs"]
        assert captured["kwargs"]["input"] == render_nft_transaction(set())
        assert captured["argv"][-1] == "sudo -n nft -f -"

    def test_vpn_manager_error_propagates(self, enabled_vpn):
        def fake_run_command(self, remote_args, timeout=None, *, stdin_data=None):
            raise VpnManagerError("SSH command failed with exit code 1")

        with patch.object(VpnManager, "run_command", fake_run_command):
            with pytest.raises(VpnManagerError):
                VpnManager().sync_forward_rules(set())

    def test_remote_command_tokens_are_safe(self, enabled_vpn):
        argv = VpnManager().build_ssh_command(["sudo", "-n", "nft", "-f", "-"])
        command = argv[-1]
        assert command == "sudo -n nft -f -"

    def test_list_forward_chain_rules_uses_read_only_nft(self, enabled_vpn):
        captured = {}

        def fake_run_command(self, remote_args, timeout=None, *, stdin_data=None):
            captured["args"] = list(remote_args)
            from app.vpn import SshResult

            return SshResult(stdout="listing", stderr="", returncode=0)

        with patch.object(VpnManager, "run_command", fake_run_command):
            output = VpnManager().list_forward_chain_rules()

        assert output == "listing"
        assert captured["args"] == [
            "sudo",
            "-n",
            "nft",
            "-a",
            "list",
            "chain",
            "inet",
            "filter",
            "forward",
        ]
        assert captured.get("stdin_data") is None

    def test_sync_passes_handle_through_to_transaction(self, enabled_vpn):
        captured = {}

        def fake_run_command(self, remote_args, timeout=None, *, stdin_data=None):
            captured["stdin"] = stdin_data
            from app.vpn import SshResult

            return SshResult(stdout="", stderr="", returncode=0)

        with patch.object(VpnManager, "run_command", fake_run_command):
            VpnManager().sync_forward_rules(
                {("10.8.0.65", "10.0.1.137")}, existing_rule_handle=42
            )

        assert "delete rule inet filter forward handle 42" in captured["stdin"]


class TestSyncVpnAuthz:
    def test_disabled_by_flag_returns_none(self, db, monkeypatch):
        monkeypatch.setattr(settings, "vpn_authz_enabled", False)
        with patch.object(vpn_authz, "VpnManager") as factory:
            assert sync_vpn_authz(db) is None
        factory.assert_not_called()

    def test_disabled_when_vpn_disabled(self, db, monkeypatch):
        monkeypatch.setattr(settings, "vpn_enabled", False)
        monkeypatch.setattr(settings, "vpn_authz_enabled", True)
        with patch.object(vpn_authz, "VpnManager") as factory:
            assert sync_vpn_authz(db) is None
        factory.assert_not_called()

    def test_dry_run_skips_gateway(self, db, monkeypatch):
        monkeypatch.setattr(settings, "vpn_enabled", True)
        monkeypatch.setattr(settings, "vpn_authz_enabled", True)
        monkeypatch.setattr(settings, "terraform_dry_run", True)
        with patch.object(vpn_authz, "VpnManager") as factory:
            assert sync_vpn_authz(db) is None
        factory.assert_not_called()

    def test_applies_desired_pairs(self, db, lab_env, authz_on):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = sync_vpn_authz(db)
        assert applied == {("10.8.0.65", "10.0.1.137")}
        manager.sync_forward_rules.assert_called_once_with(
            {("10.8.0.65", "10.0.1.137")}, existing_rule_handle=None
        )

    def test_empty_state_still_writes_valid_table(self, db, authz_on):
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = sync_vpn_authz(db)
        assert applied == set()
        manager.sync_forward_rules.assert_called_once_with(
            set(), existing_rule_handle=None
        )

    def test_gateway_failure_raises_sanitized(self, db, authz_on):
        manager = make_authz_manager()
        manager.sync_forward_rules.side_effect = VpnManagerError(
            "SSH command failed: token=hunter2hunter2hunter2"
        )
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            with pytest.raises(VpnAuthorizationError) as exc_info:
                sync_vpn_authz(db)
        message = str(exc_info.value)
        assert "token=[REDACTED]" in message
        assert "hunter2hunter2hunter2" not in message

    def test_sync_removes_stale_pair_after_stop(self, db, lab_env, authz_on):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        lab = make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            sync_vpn_authz(db)
            assert manager.sync_forward_rules.call_args_list[0][0][0] == {
                ("10.8.0.65", "10.0.1.137")
            }
            lab.status = "stopped"
            db.commit()
            sync_vpn_authz(db)
            assert manager.sync_forward_rules.call_args_list[1][0][0] == set()

    def test_concurrent_syncs_apply_consistent_state(
        self, db, lab_env, recording_vpn_manager
    ):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        lab = make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        expected = {("10.8.0.65", "10.0.1.137")}

        def worker():
            sync_vpn_authz(db)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker) for _ in range(8)]
            for future in futures:
                future.result()

        assert len(recording_vpn_manager) == 8
        assert all(applied == expected for applied in recording_vpn_manager)

        lab.status = "stopped"
        db.commit()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker) for _ in range(4)]
            for future in futures:
                future.result()

        assert recording_vpn_manager[-4:] == [set()] * 4
        assert desired_pairs(db) == set()


class TestAuthorizeLabAccess:
    def test_includes_not_yet_committed_lab(self, db, lab_env, authz_on):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            status="provisioning",
            private_ip="10.0.1.137",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_lab_access(db, lab)
        assert applied == {("10.8.0.65", "10.0.1.137")}

    def test_defers_when_learner_has_no_identity(self, db, lab_env, authz_on):
        make_identity(db, lab_env.learner_b, "10.8.0.66")
        make_lab(
            db,
            lab_env.learner_b,
            lab_env.room,
            status="running",
            private_ip="10.0.1.140",
        )
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            status="provisioning",
            private_ip="10.0.1.137",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_lab_access(db, lab)
        assert applied == {("10.8.0.66", "10.0.1.140")}

    def test_skips_revoked_identity(self, db, lab_env, authz_on):
        identity = make_identity(db, lab_env.learner_a, "10.8.0.65")
        identity.revoked_at = datetime.utcnow()
        db.commit()
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            status="provisioning",
            private_ip="10.0.1.137",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_lab_access(db, lab)
        assert applied == set()

    def test_invalid_lab_ip_raises(self, db, lab_env, authz_on):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            status="provisioning",
            private_ip="192.168.5.5",
        )
        with pytest.raises(VpnAuthorizationError):
            authorize_lab_access(db, lab)

    def test_invalid_learner_ip_skips_pair(self, db, lab_env, authz_on):
        make_identity(db, lab_env.learner_a, "999.999.999.999")
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            status="provisioning",
            private_ip="10.0.1.137",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_lab_access(db, lab)
        assert applied == set()
        manager.sync_forward_rules.assert_called_once_with(
            set(), existing_rule_handle=None
        )

    def test_disabled_returns_none(self, db, lab_env, monkeypatch):
        monkeypatch.setattr(settings, "vpn_authz_enabled", False)
        lab = make_lab(db, lab_env.learner_a, lab_env.room)
        with patch.object(vpn_authz, "VpnManager") as factory:
            assert authorize_lab_access(db, lab) is None
        factory.assert_not_called()


class TestAuthorizeBuilderAccess:
    def test_authorizes_builder_not_yet_flushed(self, engine, authz_on):
        # Regression for the deterministic autoflush=False race: the
        # just-started builder's running status/private IP are still pending
        # in the session, so desired_pairs cannot see the committed row.
        session = Session(bind=engine, autoflush=False)
        try:
            creator = make_user(session, "authz_builder_creator", role="creator")
            room = make_room(session, creator)
            make_identity(session, creator, "10.8.0.66")
            session.commit()

            builder = make_builder(
                session, creator, room, status="pending", private_ip=None
            )
            session.commit()

            builder.status = "running"
            builder.instance_id = "i-0race"
            builder.private_ip = "10.0.1.201"
            # Not flushed or committed: the persisted row is still "pending".
            assert desired_pairs(session) == set()

            manager = make_authz_manager()
            with patch.object(vpn_authz, "VpnManager", return_value=manager):
                applied = authorize_builder_access(session, builder)
            assert applied == {("10.8.0.66", "10.0.1.201")}
            manager.sync_forward_rules.assert_called_once_with(
                {("10.8.0.66", "10.0.1.201")}, existing_rule_handle=None
            )
        finally:
            session.close()

    def test_includes_not_yet_committed_builder(self, db, lab_env, authz_on):
        make_identity(db, lab_env.creator, "10.8.0.66")
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip="10.0.1.201",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == {("10.8.0.66", "10.0.1.201")}

    @pytest.mark.parametrize(
        "status", ["pending", "provisioning", "stopped", "failed"]
    )
    def test_non_running_builder_yields_no_pair(self, db, lab_env, authz_on, status):
        make_identity(db, lab_env.creator, "10.8.0.66")
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status=status,
            private_ip="10.0.1.201",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == set()
        manager.sync_forward_rules.assert_called_once_with(
            set(), existing_rule_handle=None
        )

    def test_running_builder_without_private_ip_yields_no_pair(
        self, db, lab_env, authz_on
    ):
        make_identity(db, lab_env.creator, "10.8.0.66")
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip=None,
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == set()

    def test_defers_when_creator_has_no_identity(self, db, lab_env, authz_on):
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip="10.0.1.201",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == set()

    def test_revoked_creator_identity_yields_no_pair(self, db, lab_env, authz_on):
        identity = make_identity(db, lab_env.creator, "10.8.0.66")
        identity.revoked_at = datetime.utcnow()
        db.commit()
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip="10.0.1.201",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == set()

    def test_invalid_creator_identity_skips_pair(self, db, lab_env, authz_on):
        make_identity(db, lab_env.creator, "999.999.999.999")
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip="10.0.1.201",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == set()

    def test_invalid_builder_ip_raises(self, db, lab_env, authz_on):
        make_identity(db, lab_env.creator, "10.8.0.66")
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip="192.168.5.5",
        )
        with pytest.raises(VpnAuthorizationError):
            authorize_builder_access(db, builder)

    def test_gateway_failure_raises_sanitized(self, db, lab_env, authz_on):
        make_identity(db, lab_env.creator, "10.8.0.66")
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip="10.0.1.201",
        )
        manager = make_authz_manager()
        manager.sync_forward_rules.side_effect = VpnManagerError(
            "SSH command failed: token=hunter2hunter2hunter2"
        )
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            with pytest.raises(VpnAuthorizationError) as exc_info:
                authorize_builder_access(db, builder)
        message = str(exc_info.value)
        assert "token=[REDACTED]" in message
        assert "hunter2hunter2hunter2" not in message

    def test_disabled_returns_none(self, db, lab_env, monkeypatch):
        monkeypatch.setattr(settings, "vpn_authz_enabled", False)
        builder = make_builder(db, lab_env.creator, lab_env.room)
        with patch.object(vpn_authz, "VpnManager") as factory:
            assert authorize_builder_access(db, builder) is None
        factory.assert_not_called()

    def test_committed_lab_pair_preserved(self, db, lab_env, authz_on):
        make_identity(db, lab_env.creator, "10.8.0.66")
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        builder = make_builder(
            db,
            lab_env.creator,
            lab_env.room,
            status="running",
            private_ip="10.0.1.201",
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == {
            ("10.8.0.65", "10.0.1.137"),
            ("10.8.0.66", "10.0.1.201"),
        }

    def test_backend_pair_included_when_enabled(self, db, backend_authz_on):
        creator = make_user(db, "authz_bld_backend", role="creator")
        room = make_room(db, creator)
        make_identity(db, creator, "10.8.0.66")
        make_backend(db)
        builder = make_builder(
            db, creator, room, status="running", private_ip="10.0.1.201"
        )
        manager = make_authz_manager()
        with patch.object(vpn_authz, "VpnManager", return_value=manager):
            applied = authorize_builder_access(db, builder)
        assert applied == {
            ("10.8.0.66", "10.0.1.201"),
            ("10.8.0.67", "10.0.1.201"),
        }


class TestLabManagerLifecycle:
    def test_start_authorizes_before_running_commit(
        self, db, lab_env, dry_lab_manager
    ):
        calls = []

        def fake_authorize(session, lab):
            calls.append(lab.status)
            return set()

        with patch.object(lab_manager_module, "authorize_lab_access", fake_authorize):
            lab = make_lab(
                db,
                lab_env.learner_a,
                lab_env.room,
                status="pending",
                private_ip=None,
                instance_id=None,
                security_group_id=None,
            )
            db.commit()
            dry_lab_manager.start_lab_instance(lab, db)

        assert calls == ["provisioning"]
        assert lab.status == "running"
        assert lab.private_ip == "10.0.1.100"

    def test_start_authz_failure_marks_failed_and_sanitizes(
        self, db, lab_env, dry_lab_manager
    ):
        def fake_authorize(session, lab):
            raise VpnAuthorizationError(
                "VPN authorization update failed: token=supersecretvalue999"
            )

        with patch.object(lab_manager_module, "authorize_lab_access", fake_authorize):
            lab = make_lab(
                db,
                lab_env.learner_a,
                lab_env.room,
                status="pending",
                private_ip=None,
                instance_id=None,
                security_group_id=None,
            )
            db.commit()
            with pytest.raises(VpnAuthorizationError):
                dry_lab_manager.start_lab_instance(lab, db)

        assert lab.status == "failed"
        assert "VPN authorization update failed" in lab.error_message
        assert "token=[REDACTED]" in lab.error_message
        assert "supersecretvalue999" not in lab.error_message

    def test_start_disabled_authz_skips_gateway(
        self, db, lab_env, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings, "vpn_authz_enabled", False)
        monkeypatch.setattr(settings, "vpn_enabled", True)
        monkeypatch.setattr(settings, "terraform_dry_run", False)
        monkeypatch.setattr(
            settings, "lab_terraform_workspace_root", str(tmp_path / "labs")
        )
        monkeypatch.setattr(
            settings, "terraform_plugin_cache_dir", str(tmp_path / "plugins")
        )
        manager = LabManager()
        manager.config["terraform_dry_run"] = True
        with patch.object(vpn_authz, "VpnManager") as factory:
            lab = make_lab(
                db,
                lab_env.learner_a,
                lab_env.room,
                status="pending",
                private_ip=None,
                instance_id=None,
                security_group_id=None,
            )
            db.commit()
            manager.start_lab_instance(lab, db)
        factory.assert_not_called()
        assert lab.status == "running"

    def test_stop_syncs_after_stopping_commit(self, db, lab_env, dry_lab_manager):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        make_identity(db, lab_env.learner_b, "10.8.0.66")
        make_lab(db, lab_env.learner_b, lab_env.room, private_ip="10.0.1.140")
        lab = make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        db.commit()
        captured = []

        def fake_sync(session):
            captured.append(desired_pairs(session))
            return captured[-1]

        with patch.object(lab_manager_module, "sync_vpn_authz", fake_sync):
            dry_lab_manager.stop_lab_instance(lab, db)

        assert lab.status == "stopped"
        assert lab.private_ip is None
        assert captured == [{("10.8.0.66", "10.0.1.140")}]

    def test_stop_continues_when_sync_fails(
        self, db, lab_env, dry_lab_manager, caplog
    ):
        lab = make_lab(db, lab_env.learner_a, lab_env.room, private_ip="10.0.1.137")
        db.commit()

        def fake_sync(session):
            raise VpnAuthorizationError("VPN authorization sync is busy")

        with patch.object(lab_manager_module, "sync_vpn_authz", fake_sync):
            with caplog.at_level("WARNING", logger="app.lab_manager"):
                dry_lab_manager.stop_lab_instance(lab, db)

        assert lab.status == "stopped"
        assert any("VPN authorization removal failed" in r.message for r in caplog.records)

    def test_repeated_start_stop_idempotent(
        self, db, lab_env, dry_lab_manager
    ):
        applied = []
        with patch.object(
            lab_manager_module,
            "authorize_lab_access",
            lambda session, lab: applied.append(("start", lab.status)),
        ):
            lab = make_lab(
                db,
                lab_env.learner_a,
                lab_env.room,
                status="pending",
                private_ip=None,
                instance_id=None,
                security_group_id=None,
            )
            db.commit()
            dry_lab_manager.start_lab_instance(lab, db)
        assert lab.status == "running"

        with patch.object(
            lab_manager_module,
            "sync_vpn_authz",
            lambda session: applied.append(("stop-sync", session.get(LabInstance, lab.id).status)),
        ):
            dry_lab_manager.stop_lab_instance(lab, db)
        assert lab.status == "stopped"
        assert applied[0] == ("start", "provisioning")
        assert applied[-1] == ("stop-sync", "stopping")


class TestReconciliationLifecycle:
    @pytest.fixture
    def worker(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "terraform_dry_run", True)
        monkeypatch.setattr(
            settings, "lab_terraform_workspace_root", str(tmp_path / "labs")
        )
        monkeypatch.setattr(
            settings, "terraform_plugin_cache_dir", str(tmp_path / "plugins")
        )
        return ReconciliationWorker()

    def test_expired_lab_revokes_authorization(
        self, db, lab_env, worker, monkeypatch
    ):
        make_identity(db, lab_env.learner_a, "10.8.0.65")
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            private_ip="10.0.1.137",
            expires_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.commit()
        captured = []
        monkeypatch.setattr(
            lab_manager_module,
            "sync_vpn_authz",
            lambda session: captured.append(desired_pairs(session)),
        )
        asyncio.run(worker._handle_expired_running_labs(db))
        db.expire_all()
        assert lab.status == "stopped"
        assert captured and captured[-1] == set()
        assert desired_pairs(db) == set()

    def test_stale_provisioning_revokes_authorization(
        self, db, lab_env, worker, monkeypatch
    ):
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            status="provisioning",
            private_ip="10.0.1.137",
            updated_at=datetime.utcnow() - timedelta(minutes=30),
        )
        db.commit()
        captured = []
        monkeypatch.setattr(
            lab_manager_module,
            "sync_vpn_authz",
            lambda session: captured.append(desired_pairs(session)),
        )
        asyncio.run(worker._handle_stale_provisioning_labs(db))
        db.expire_all()
        assert lab.status == "stopped"
        assert captured

    def test_crashed_stopping_revokes_authorization(
        self, db, lab_env, worker, monkeypatch
    ):
        lab = make_lab(
            db,
            lab_env.learner_a,
            lab_env.room,
            status="stopping",
            private_ip="10.0.1.137",
            updated_at=datetime.utcnow() - timedelta(seconds=2000),
        )
        db.commit()
        captured = []
        monkeypatch.setattr(
            lab_manager_module,
            "sync_vpn_authz",
            lambda session: captured.append(desired_pairs(session)),
        )
        asyncio.run(worker._handle_crashed_stopping_labs(db))
        db.expire_all()
        assert lab.status == "stopped"
        assert captured

    def test_reconcile_pass_runs_authz_sync(self, db, worker, monkeypatch):
        called = []
        routes_called = []
        monkeypatch.setattr(reconciliation_module, "SessionLocal", lambda: db)
        monkeypatch.setattr(
            reconciliation_module,
            "sync_vpn_authz",
            lambda session: called.append(True),
        )
        monkeypatch.setattr(
            reconciliation_module,
            "sync_backend_routes",
            lambda session: routes_called.append(True) or (set(), set()),
        )
        asyncio.run(worker._reconcile_once())
        assert called == [True]
        assert routes_called == [True]

    def test_reconcile_sync_failure_is_nonfatal(self, db, worker, monkeypatch):
        def boom(session):
            raise VpnAuthorizationError("gateway unreachable")

        monkeypatch.setattr(reconciliation_module, "SessionLocal", lambda: db)
        monkeypatch.setattr(reconciliation_module, "sync_vpn_authz", boom)
        monkeypatch.setattr(
            reconciliation_module, "sync_backend_routes", lambda session: (set(), set())
        )
        asyncio.run(worker._reconcile_once())

    def test_stop_guard_conflict_raises_operation_in_progress(
        self, db, lab_env, dry_lab_manager
    ):
        lab = make_lab(db, lab_env.learner_a, lab_env.room)
        db.commit()
        with lab_manager_module.single_flight(
            lab_manager_module.lab_lock_key(lab.id)
        ):
            with pytest.raises(OperationInProgress):
                dry_lab_manager.stop_lab_instance(lab, db)


class TestProfileEndpointSync:
    @pytest.fixture
    def pg_env(self):
        suffix = uuid4().hex[:8]
        session = SessionLocal()
        learner = User(
            username=f"vpn_authz_{suffix}",
            email=f"vpn_authz_{suffix}@example.com",
            password_hash="not-used",
            role="learner",
        )
        creator = User(
            username=f"vpn_authz_c_{suffix}",
            email=f"vpn_authz_c_{suffix}@example.com",
            password_hash="not-used",
            role="creator",
        )
        session.add_all([learner, creator])
        session.flush()
        room = Room(
            creator_id=creator.id,
            name=f"vpn_authz_room_{suffix}",
            category="web",
            difficulty="beginner",
            status="published",
        )
        session.add(room)
        session.flush()
        lab = LabInstance(
            room_id=room.id,
            learner_id=learner.id,
            status="running",
            private_ip="10.0.1.150",
            instance_id="i-authztest",
            security_group_id="sg-authztest",
            expires_at=datetime.utcnow() + timedelta(hours=2),
        )
        session.add(lab)
        session.commit()
        learner_id, creator_id, room_id, lab_id = (
            learner.id,
            creator.id,
            room.id,
            lab.id,
        )
        session.close()
        yield SimpleNamespace(
            learner_id=learner_id, creator_id=creator_id, room_id=room_id, lab_id=lab_id
        )
        cleanup = SessionLocal()
        try:
            cleanup.execute(delete(LabInstance).where(LabInstance.id == lab_id))
            cleanup.execute(delete(VpnIdentity).where(VpnIdentity.user_id == learner_id))
            cleanup.execute(delete(Room).where(Room.id == room_id))
            cleanup.execute(delete(User).where(User.id.in_([learner_id, creator_id])))
            cleanup.commit()
        finally:
            cleanup.close()

    def test_profile_creation_triggers_sync_with_lab_pair(
        self, pg_env, key_file, monkeypatch
    ):
        monkeypatch.setattr(settings, "vpn_enabled", True)
        monkeypatch.setattr(settings, "vpn_authz_enabled", True)
        monkeypatch.setattr(settings, "vpn_gateway_ssh_key_path", key_file)
        monkeypatch.setattr(settings, "terraform_dry_run", False)

        manager = make_authz_manager()
        manager.read_ca_certificate.return_value = CA_PEM
        manager.read_client_certificate.return_value = CERT_PEM
        manager.read_client_key.return_value = KEY_PEM
        manager.read_tls_crypt_key.return_value = TA_PEM
        monkeypatch.setattr(vpn_routes, "VpnManager", MagicMock(return_value=manager))

        applied: list[set] = []
        authz_manager = make_authz_manager()
        authz_manager.sync_forward_rules.side_effect = (
            lambda pairs, existing_rule_handle=None: applied.append(set(pairs))
        )
        monkeypatch.setattr(
            vpn_authz, "VpnManager", MagicMock(return_value=authz_manager)
        )

        token = create_access_token(pg_env.learner_id, "learner")
        response = client.get(
            "/api/vpn/profile", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200

        session = SessionLocal()
        try:
            identity = session.scalar(
                select(VpnIdentity).where(VpnIdentity.user_id == pg_env.learner_id)
            )
            assert identity is not None
        finally:
            session.close()

        assert applied, "sync was not triggered by profile issuance"
        assert (identity.vpn_ip, "10.0.1.150") in applied[-1]

    def test_profile_sync_failure_does_not_break_download(
        self, pg_env, key_file, monkeypatch
    ):
        monkeypatch.setattr(settings, "vpn_enabled", True)
        monkeypatch.setattr(settings, "vpn_authz_enabled", True)
        monkeypatch.setattr(settings, "vpn_gateway_ssh_key_path", key_file)

        manager = make_authz_manager()
        manager.read_ca_certificate.return_value = CA_PEM
        manager.read_client_certificate.return_value = CERT_PEM
        manager.read_client_key.return_value = KEY_PEM
        manager.read_tls_crypt_key.return_value = TA_PEM
        monkeypatch.setattr(vpn_routes, "VpnManager", MagicMock(return_value=manager))

        authz_manager = make_authz_manager()
        authz_manager.sync_forward_rules.side_effect = VpnManagerError(
            "gateway unreachable"
        )
        monkeypatch.setattr(
            vpn_authz, "VpnManager", MagicMock(return_value=authz_manager)
        )

        token = create_access_token(pg_env.learner_id, "learner")
        response = client.get(
            "/api/vpn/profile", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-openvpn-profile"


class TestExistingVpnSafety:
    def test_transaction_never_touches_openvpn_config(self):
        tx = render_nft_transaction({("10.8.0.65", "10.0.1.137")})
        lowered = tx.lower()
        assert "openvpn" not in lowered
        assert "ccd" not in lowered
        assert "ta.key" not in lowered
        assert "server.conf" not in lowered
        assert "easy-rsa" not in lowered

    def test_sync_command_cannot_reach_openvpn_paths(self, enabled_vpn):
        captured = {}

        def fake_run_command(self, remote_args, timeout=None, *, stdin_data=None):
            captured["args"] = list(remote_args)
            from app.vpn import SshResult

            return SshResult(stdout="", stderr="", returncode=0)

        with patch.object(VpnManager, "run_command", fake_run_command):
            VpnManager().sync_forward_rules({("10.8.0.65", "10.0.1.137")})

        assert captured["args"] == ["sudo", "-n", "nft", "-f", "-"]
        joined = " ".join(captured["args"])
        assert "openvpn" not in joined
        assert "/etc" not in joined

    def test_validate_vpn_ip_still_excludes_test_client(self):
        with pytest.raises(VpnManagerError):
            validate_vpn_ip("10.8.0.20")

    def test_learner_ip_validation_unchanged(self):
        assert validate_vpn_ip("10.8.0.65") == "10.8.0.65"


class TestNoShellAnywhere:
    def test_vpn_module_has_no_shell_true(self):
        import inspect
        import app.vpn as vpn_module

        source = inspect.getsource(vpn_module)
        assert "shell=True" not in source

    def test_vpn_authz_module_has_no_shell_true(self):
        import inspect

        source = inspect.getsource(vpn_authz)
        assert "shell=True" not in source
        assert "subprocess" not in source

    def test_no_global_flush_in_authz_code(self):
        import inspect

        for module in (vpn_authz, lab_manager_module):
            source = inspect.getsource(module)
            assert "flush ruleset" not in source
