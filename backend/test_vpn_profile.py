#!/usr/bin/env python3
"""Tests for learner VPN profile issuance (Phase 2). All gateway calls are mocked."""

import logging
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import ipaddress
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import vpn_routes
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import User, VpnIdentity
from app.security import create_access_token
from app.vpn import (
    VpnManager,
    VpnManagerError,
    build_client_profile,
    common_name_for_user,
    iter_candidate_ips,
    render_client_profile,
    sanitize_error,
    validate_common_name,
    validate_vpn_ip,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def vpn_authz_off(monkeypatch):
    monkeypatch.setattr(settings, "vpn_authz_enabled", False)

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


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get_profile(token: str | None = None, params: dict | None = None):
    headers = auth_header(token) if token else {}
    return client.get("/api/vpn/profile", headers=headers, params=params)


def _username(user_id):
    with SessionLocal() as db:
        return db.get(User, user_id).username


def identity_rows(user_id: int) -> list[VpnIdentity]:
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(VpnIdentity).where(VpnIdentity.user_id == user_id)
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)
    finally:
        session.close()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "test_id.pem"
    path.write_text("placeholder-not-a-real-key\n")
    return str(path)


@pytest.fixture
def vpn_on(monkeypatch, key_file):
    monkeypatch.setattr(settings, "vpn_enabled", True)
    monkeypatch.setattr(settings, "vpn_gateway_ssh_key_path", key_file)


@pytest.fixture
def mock_manager(vpn_on, monkeypatch):
    manager = MagicMock(spec=VpnManager)
    manager.read_ca_certificate.return_value = CA_PEM
    manager.read_client_certificate.return_value = CERT_PEM
    manager.read_client_key.return_value = KEY_PEM
    manager.read_tls_crypt_key.return_value = TA_PEM
    factory = MagicMock(return_value=manager)
    monkeypatch.setattr(vpn_routes, "VpnManager", factory)
    manager.factory = factory
    return manager


@pytest.fixture
def env():
    suffix = uuid4().hex[:8]
    session = SessionLocal()
    learner = User(
        username=f"vpn_api_learner_{suffix}",
        email=f"vpn_api_learner_{suffix}@example.com",
        password_hash="not-used",
        role="learner",
    )
    learner_two = User(
        username=f"vpn_api_learner_two_{suffix}",
        email=f"vpn_api_learner_two_{suffix}@example.com",
        password_hash="not-used",
        role="learner",
    )
    creator = User(
        username=f"vpn_api_creator_{suffix}",
        email=f"vpn_api_creator_{suffix}@example.com",
        password_hash="not-used",
        role="creator",
    )
    admin = User(
        username=f"vpn_api_admin_{suffix}",
        email=f"vpn_api_admin_{suffix}@example.com",
        password_hash="not-used",
        role="admin",
    )
    session.add_all([learner, learner_two, creator, admin])
    session.commit()

    environment = SimpleNamespace(
        learner_id=learner.id,
        learner_two_id=learner_two.id,
        creator_id=creator.id,
        admin_id=admin.id,
        learner_token=create_access_token(learner.id, learner.role),
        learner_two_token=create_access_token(learner_two.id, learner_two.role),
        creator_token=create_access_token(creator.id, creator.role),
        admin_token=create_access_token(admin.id, admin.role),
        user_ids=[learner.id, learner_two.id, creator.id, admin.id],
    )
    session.close()

    try:
        yield environment
    finally:
        cleanup = SessionLocal()
        try:
            cleanup.execute(
                delete(VpnIdentity)
                .where(VpnIdentity.user_id.in_(environment.user_ids))
                .execution_options(synchronize_session=False)
            )
            cleanup.execute(
                delete(User)
                .where(User.id.in_(environment.user_ids))
                .execution_options(synchronize_session=False)
            )
            cleanup.commit()
        finally:
            cleanup.close()


class TestAuthorization:
    def test_requires_authentication(self):
        response = get_profile()
        assert response.status_code in (401, 403)

    def test_invalid_token_rejected(self):
        response = get_profile("not-a-valid-token")
        assert response.status_code in (401, 403)

    def test_creator_gets_own_profile(self, env, mock_manager):
        response = get_profile(env.creator_token)
        assert response.status_code == 200
        mock_manager.read_client_certificate.assert_called_with(
            f"sf-{env.creator_id}"
        )
        assert identity_rows(env.creator_id)
        assert identity_rows(env.learner_id) == []

    def test_admin_gets_own_profile(self, env, mock_manager):
        response = get_profile(env.admin_token)
        assert response.status_code == 200
        mock_manager.read_client_certificate.assert_called_with(
            f"sf-{env.admin_id}"
        )

    def test_learner_gets_profile(self, env, mock_manager):
        response = get_profile(env.learner_token)
        assert response.status_code == 200

    def test_user_id_query_param_ignored(self, env, mock_manager):
        response = get_profile(
            env.learner_token, params={"user_id": env.learner_two_id}
        )
        assert response.status_code == 200
        mock_manager.read_client_certificate.assert_called_with(
            f"sf-{env.learner_id}"
        )
        assert identity_rows(env.learner_id)
        assert identity_rows(env.learner_two_id) == []


class TestDisabledService:
    def test_vpn_disabled_returns_503_and_never_instantiates_manager(
        self, env, monkeypatch
    ):
        monkeypatch.setattr(settings, "vpn_enabled", False)
        factory = MagicMock()
        monkeypatch.setattr(vpn_routes, "VpnManager", factory)
        response = get_profile(env.learner_token)
        assert response.status_code == 503
        factory.assert_not_called()

    def test_vpn_gateway_port_default(self):
        assert settings.vpn_gateway_port == 1194


class TestIdentityLifecycle:
    def test_first_request_creates_identity(self, env, mock_manager):
        response = get_profile(env.learner_token)
        assert response.status_code == 200
        rows = identity_rows(env.learner_id)
        assert len(rows) == 1
        assert rows[0].common_name == f"sf-{env.learner_id}"
        assert rows[0].revoked_at is None
        mock_manager.issue_client_certificate.assert_called_once_with(
            f"sf-{env.learner_id}"
        )
        mock_manager.write_client_ccd.assert_called_once()

    def test_second_request_reuses_identity(self, env, mock_manager):
        assert get_profile(env.learner_token).status_code == 200
        first_ip = identity_rows(env.learner_id)[0].vpn_ip
        assert get_profile(env.learner_token).status_code == 200
        rows = identity_rows(env.learner_id)
        assert len(rows) == 1
        assert rows[0].vpn_ip == first_ip
        assert mock_manager.issue_client_certificate.call_count == 1

    def test_common_name_is_sf_user_id(self, env, mock_manager):
        get_profile(env.learner_token)
        row = identity_rows(env.learner_id)[0]
        assert row.common_name == f"sf-{env.learner_id}"
        assert re.fullmatch(r"sf-[1-9][0-9]*", row.common_name)

    def test_username_and_email_do_not_influence_common_name(
        self, env, mock_manager, key_file
    ):
        suffix = uuid4().hex[:8]
        session = SessionLocal()
        tricky = User(
            username=f"sf-{env.learner_two_id}",
            email=f"sf-9999-{suffix}@example.com",
            password_hash="not-used",
            role="learner",
        )
        session.add(tricky)
        session.commit()
        token = create_access_token(tricky.id, tricky.role)
        user_id = tricky.id
        session.close()
        try:
            response = get_profile(token)
            assert response.status_code == 200
            row = identity_rows(user_id)[0]
            assert row.common_name == f"sf-{user_id}"
            assert row.common_name != f"sf-{env.learner_two_id}"
        finally:
            cleanup = SessionLocal()
            try:
                cleanup.execute(
                    delete(VpnIdentity)
                    .where(VpnIdentity.user_id == user_id)
                    .execution_options(synchronize_session=False)
                )
                cleanup.execute(
                    delete(User)
                    .where(User.id == user_id)
                    .execution_options(synchronize_session=False)
                )
                cleanup.commit()
            finally:
                cleanup.close()

    def test_ip_in_learner_range(self, env, mock_manager):
        get_profile(env.learner_token)
        ip = identity_rows(env.learner_id)[0].vpn_ip
        network = ipaddress.ip_network(settings.vpn_learner_ip_range)
        address = ipaddress.ip_address(ip)
        assert address in network
        assert address not in (network.network_address, network.broadcast_address)

    def test_ip_excludes_test_client_address(self, env, mock_manager):
        get_profile(env.learner_token)
        ip = identity_rows(env.learner_id)[0].vpn_ip
        assert ip != "10.8.0.20"

    def test_two_learners_get_distinct_ips(self, env, mock_manager):
        assert get_profile(env.learner_token).status_code == 200
        assert get_profile(env.learner_two_token).status_code == 200
        ip_one = identity_rows(env.learner_id)[0].vpn_ip
        ip_two = identity_rows(env.learner_two_id)[0].vpn_ip
        assert ip_one != ip_two


class TestRevokedIdentity:
    def test_revoked_identity_forbidden(self, env, mock_manager):
        session = SessionLocal()
        session.add(
            VpnIdentity(
                user_id=env.learner_id,
                common_name=f"sf-{env.learner_id}",
                vpn_ip="10.8.0.70",
                revoked_at=datetime.utcnow(),
            )
        )
        session.commit()
        session.close()
        response = get_profile(env.learner_token)
        assert response.status_code == 403
        assert response.json()["detail"] == "VPN access for this account has been disabled"
        mock_manager.read_client_certificate.assert_not_called()
        mock_manager.issue_client_certificate.assert_not_called()


class TestAllocationRace:
    def test_ip_conflict_retries_next_address(self, env, mock_manager, monkeypatch):
        session = SessionLocal()
        session.add(
            VpnIdentity(
                user_id=env.learner_two_id,
                common_name=f"sf-{env.learner_two_id}",
                vpn_ip="10.8.0.70",
            )
        )
        session.commit()
        session.close()
        monkeypatch.setattr(
            vpn_routes,
            "_candidate_ips",
            lambda db: ["10.8.0.70", "10.8.0.71"],
        )
        response = get_profile(env.learner_token)
        assert response.status_code == 200
        assert identity_rows(env.learner_id)[0].vpn_ip == "10.8.0.71"

    def test_stale_candidate_list_retries_on_conflict(
        self, env, mock_manager, monkeypatch
    ):
        session = SessionLocal()
        session.add(
            VpnIdentity(
                user_id=env.learner_two_id,
                common_name=f"sf-{env.learner_two_id}",
                vpn_ip="10.8.0.72",
            )
        )
        session.commit()
        session.close()
        monkeypatch.setattr(
            vpn_routes,
            "_candidate_ips",
            lambda db: ["10.8.0.72", "10.8.0.73"],
        )
        response = get_profile(env.learner_token)
        assert response.status_code == 200
        assert identity_rows(env.learner_id)[0].vpn_ip == "10.8.0.73"

    def test_same_user_race_returns_winner_and_heals_ccd(
        self, env, mock_manager, monkeypatch
    ):
        inserted = {"done": False}

        def ccd_side_effect(common_name, vpn_ip):
            if not inserted["done"]:
                inserted["done"] = True
                session = SessionLocal()
                try:
                    session.add(
                        VpnIdentity(
                            user_id=env.learner_id,
                            common_name=common_name,
                            vpn_ip="10.8.0.99",
                        )
                    )
                    session.commit()
                finally:
                    session.close()

        mock_manager.write_client_ccd.side_effect = ccd_side_effect
        monkeypatch.setattr(
            vpn_routes, "_candidate_ips", lambda db: ["10.8.0.65"]
        )
        response = get_profile(env.learner_token)
        assert response.status_code == 200
        rows = identity_rows(env.learner_id)
        assert len(rows) == 1
        assert rows[0].vpn_ip == "10.8.0.99"
        mock_manager.write_client_ccd.assert_any_call(
            f"sf-{env.learner_id}", "10.8.0.99"
        )

    def test_unique_constraints_enforced(self, env):
        session = SessionLocal()
        try:
            session.add(
                VpnIdentity(
                    user_id=env.learner_id,
                    common_name=f"sf-{env.learner_id}",
                    vpn_ip="10.8.0.120",
                )
            )
            session.commit()
            session.add(
                VpnIdentity(
                    user_id=env.learner_two_id,
                    common_name=f"sf-{env.learner_two_id}",
                    vpn_ip="10.8.0.120",
                )
            )
            with pytest.raises(Exception) as excinfo:
                session.commit()
            assert "unique" in str(excinfo.value).lower() or isinstance(
                excinfo.value, IntegrityError
            )
        finally:
            session.rollback()
            session.close()


class TestPoolExhaustion:
    def test_pool_exhausted_returns_503(self, env, mock_manager, monkeypatch):
        monkeypatch.setattr(vpn_routes, "_candidate_ips", lambda db: [])
        response = get_profile(env.learner_token)
        assert response.status_code == 503
        assert identity_rows(env.learner_id) == []


class TestGatewayFailures:
    def test_issuance_failure_returns_502_and_no_row(self, env, mock_manager):
        mock_manager.issue_client_certificate.side_effect = VpnManagerError(
            "SSH command failed with exit code 1"
        )
        mock_manager.read_client_certificate.side_effect = VpnManagerError(
            "SSH command failed with exit code 2"
        )
        response = get_profile(env.learner_token)
        assert response.status_code == 502
        assert response.json()["detail"] == "VPN gateway operation failed"
        assert "/etc/openvpn" not in response.text
        assert "easyrsa" not in response.text
        assert identity_rows(env.learner_id) == []

    def test_existing_certificate_reused_when_issue_fails(
        self, env, mock_manager
    ):
        mock_manager.issue_client_certificate.side_effect = VpnManagerError(
            "SSH command failed with exit code 1"
        )
        response = get_profile(env.learner_token)
        assert response.status_code == 200
        assert len(identity_rows(env.learner_id)) == 1

    def test_ccd_failure_returns_502_and_no_row(self, env, mock_manager):
        mock_manager.write_client_ccd.side_effect = VpnManagerError(
            "SSH command failed with exit code 1"
        )
        response = get_profile(env.learner_token)
        assert response.status_code == 502
        assert identity_rows(env.learner_id) == []

    def test_profile_read_failure_after_identity_created(
        self, env, mock_manager
    ):
        mock_manager.read_ca_certificate.side_effect = VpnManagerError(
            "SSH command failed with exit code 1"
        )
        response = get_profile(env.learner_token)
        assert response.status_code == 502
        assert len(identity_rows(env.learner_id)) == 1
        mock_manager.read_ca_certificate.side_effect = None
        mock_manager.read_ca_certificate.return_value = CA_PEM
        response = get_profile(env.learner_token)
        assert response.status_code == 200
        assert mock_manager.issue_client_certificate.call_count == 1


class TestProfileContent:
    def test_profile_contains_required_directives(self, env, mock_manager):
        response = get_profile(env.learner_token)
        profile = response.text
        expected_host = settings.vpn_gateway_host
        expected_port = settings.vpn_gateway_port
        for line in (
            "client\n",
            "dev tun\n",
            "proto udp\n",
            f"remote {expected_host} {expected_port}\n",
            "resolv-retry infinite\n",
            "nobind\n",
            "persist-key\n",
            "persist-tun\n",
            "remote-cert-tls server\n",
            "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305\n",
            "auth SHA256\n",
            "tls-version-min 1.2\n",
            "verb 3\n",
        ):
            assert line in profile

    def test_profile_contains_inline_blocks(self, env, mock_manager):
        profile = get_profile(env.learner_token).text
        assert "<ca>\n" in profile
        assert "</ca>\n" in profile
        assert "<cert>\n" in profile
        assert "</cert>\n" in profile
        assert "<key>\n" in profile
        assert "</key>\n" in profile
        assert "<tls-crypt>\n" in profile
        assert "</tls-crypt>\n" in profile
        assert CA_PEM.strip() in profile
        assert CERT_PEM.strip() in profile
        assert KEY_PEM.strip() in profile
        assert TA_PEM.strip() in profile

    def test_profile_has_no_password_auth(self, env, mock_manager):
        profile = get_profile(env.learner_token).text
        assert "auth-user-pass" not in profile

    def test_profile_has_no_redirect_gateway(self, env, mock_manager):
        profile = get_profile(env.learner_token).text
        assert "redirect-gateway" not in profile
        assert "route 0.0.0.0" not in profile

    def test_profile_contains_single_private_key(self, env, mock_manager):
        profile = get_profile(env.learner_token).text
        assert profile.count("-----BEGIN PRIVATE KEY-----") == 1

    def test_profile_excludes_ca_and_server_keys(self, env, mock_manager):
        profile = get_profile(env.learner_token).text
        assert "CA-PRIVATE-KEY" not in profile
        assert "SERVER-PRIVATE-KEY" not in profile

    def test_no_method_exposes_ca_or_server_keys(self):
        for name in (
            "read_ca_key",
            "read_server_key",
            "read_ca_private_key",
            "read_server_private_key",
        ):
            assert not hasattr(VpnManager, name)

    def test_response_headers(self, env, mock_manager):
        response = get_profile(env.learner_token)
        assert response.headers["content-type"].startswith(
            "application/x-openvpn-profile"
        )
        assert (
            response.headers["content-disposition"]
            == f'attachment; filename="{vpn_routes.vpn_profile_filename(_username(env.learner_id))}"'
        )
        assert response.headers["cache-control"] == "no-store"


class TestLogging:
    def test_key_material_not_logged_on_success(self, env, mock_manager, caplog):
        with caplog.at_level(logging.DEBUG):
            response = get_profile(env.learner_token)
        assert response.status_code == 200
        assert "LEARNER-KEY-FIXTURE" not in caplog.text
        assert "-----BEGIN PRIVATE KEY-----" not in caplog.text
        assert "<key>" not in caplog.text

    def test_sanitized_errors_logged_without_material(
        self, env, mock_manager, caplog
    ):
        dirty = (
            "boom password=hunter2 "
            "-----BEGIN PRIVATE KEY-----\nZm9v\n-----END PRIVATE KEY-----"
        )
        mock_manager.issue_client_certificate.side_effect = VpnManagerError(
            sanitize_error(dirty)
        )
        mock_manager.read_client_certificate.side_effect = VpnManagerError(
            sanitize_error(dirty)
        )
        with caplog.at_level(logging.DEBUG):
            response = get_profile(env.learner_token)
        assert response.status_code == 502
        assert "VPN profile request failed" in caplog.text
        assert "hunter2" not in caplog.text
        assert "BEGIN PRIVATE KEY" not in caplog.text


class TestSubprocessSafety:
    @pytest.fixture
    def real_manager(self, vpn_on):
        return VpnManager()

    def test_issue_rejects_bad_common_names(self, real_manager, monkeypatch):
        run = MagicMock()
        monkeypatch.setattr("app.vpn.subprocess.run", run)
        for bad in (
            "sf-1;reboot",
            "$(id)",
            "sf-1 ../x",
            "sf-0",
            "sf-01",
            "SF-1",
            "user-1",
            "sf-",
            "sf-1 x",
            "sf-1\ncat",
            "",
        ):
            with pytest.raises(VpnManagerError):
                real_manager.issue_client_certificate(bad)
        run.assert_not_called()

    def test_write_ccd_rejects_bad_ips(self, real_manager, monkeypatch):
        run = MagicMock()
        monkeypatch.setattr("app.vpn.subprocess.run", run)
        for bad_ip in ("10.8.0.20", "10.8.0.64", "10.8.0.127", "10.9.0.65",
                       "10.8.0.999", "not-an-ip", "10.8.0.65;reboot"):
            with pytest.raises(VpnManagerError):
                real_manager.write_client_ccd("sf-1", bad_ip)
        run.assert_not_called()

    def test_read_rejects_path_traversal_cn(self, real_manager, monkeypatch):
        run = MagicMock()
        monkeypatch.setattr("app.vpn.subprocess.run", run)
        for bad in ("sf-1/../server", "../../etc/passwd", "sf-1;cat", "sf-1.."):
            with pytest.raises(VpnManagerError):
                real_manager.read_client_certificate(bad)
            with pytest.raises(VpnManagerError):
                real_manager.read_client_key(bad)
        run.assert_not_called()

    def test_common_name_for_user_rejects_bad_ids(self):
        for bad in (0, -1, True, 1.5, "1", None):
            with pytest.raises(VpnManagerError):
                common_name_for_user(bad)
        assert common_name_for_user(42) == "sf-42"

    def test_validate_helpers(self):
        assert validate_common_name("sf-1") == "sf-1"
        for bad in ("sf-0", "sf-01", "sf-1 ", " sf-1", "sf-1\n", "sf-9999x"):
            with pytest.raises(VpnManagerError):
                validate_common_name(bad)
        assert validate_vpn_ip("10.8.0.65") == "10.8.0.65"
        for bad in ("10.8.0.20", "10.8.0.64", "10.8.0.127", "10.0.1.5",
                    "10.8.0.65 ", "999.1.1.1"):
            with pytest.raises(VpnManagerError):
                validate_vpn_ip(bad)

    def test_iter_candidate_ips(self):
        candidates = list(iter_candidate_ips(set()))
        assert candidates[0] == "10.8.0.65"
        assert candidates[-1] == "10.8.0.126"
        assert len(candidates) == 62
        assert "10.8.0.20" not in candidates
        assert "10.8.0.64" not in candidates
        assert "10.8.0.127" not in candidates
        reduced = list(iter_candidate_ips({"10.8.0.65"}))
        assert "10.8.0.65" not in reduced
        assert len(reduced) == 61

    def test_render_profile_validates_inputs(self):
        kwargs = dict(
            host="13.53.64.174",
            port=1194,
            ca_certificate=CA_PEM,
            client_certificate=CERT_PEM,
            client_key=KEY_PEM,
            tls_crypt_key=TA_PEM,
        )
        with pytest.raises(VpnManagerError):
            render_client_profile(**{**kwargs, "host": "bad host"})
        with pytest.raises(VpnManagerError):
            render_client_profile(**{**kwargs, "port": 0})
        with pytest.raises(VpnManagerError):
            render_client_profile(**{**kwargs, "client_key": "no-pem"})
        with pytest.raises(VpnManagerError):
            build_client_profile(MagicMock(), "bad-cn", "10.8.0.65")
        with pytest.raises(VpnManagerError):
            build_client_profile(MagicMock(), "sf-1", "10.0.1.5")

    def test_full_flow_uses_safe_argv_only(self, env, vpn_on, key_file, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            remote = argv[-1]
            if "build-client-full" in remote:
                stdout = ""
            elif "/issued/" in remote:
                stdout = CERT_PEM
            elif "/private/" in remote:
                stdout = KEY_PEM
            elif "ca.crt" in remote:
                stdout = CA_PEM
            elif "ta.key" in remote:
                stdout = TA_PEM
            elif "tee" in remote:
                stdout = kwargs.get("input", "")
            else:
                stdout = ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr("app.vpn.subprocess.run", fake_run)
        response = get_profile(env.learner_token)
        assert response.status_code == 200
        assert calls, "expected gateway commands to be issued"

        safe_token = re.compile(r"[A-Za-z0-9_./:=@%+-]+\Z")
        issue_remote = None
        ccd_kwargs = None
        for argv, kwargs in calls:
            assert argv[0] == "ssh"
            assert kwargs.get("shell") in (None, False)
            assert kwargs["timeout"] > 0
            assert "BatchMode=yes" in argv
            assert "StrictHostKeyChecking=yes" in argv
            assert "IdentitiesOnly=yes" in argv
            assert key_file in argv
            remote = argv[-1]
            assert remote == argv[-1]
            assert isinstance(remote, str)
            assert ".." not in remote
            assert "secforge-test-client" not in remote
            for token in remote.split():
                assert safe_token.match(token), token
            if "build-client-full" in remote:
                issue_remote = remote
            if "tee" in remote:
                ccd_kwargs = kwargs
        assert issue_remote is not None
        assert "sudo" in issue_remote.split()
        assert f"sf-{env.learner_id}" in issue_remote.split()
        assert "nopass" in issue_remote.split()
        assert f"--vars={'/etc/openvpn/easy-rsa/vars'}" in issue_remote.split()
        assert ccd_kwargs is not None
        ip = identity_rows(env.learner_id)[0].vpn_ip
        assert ccd_kwargs["input"] == f"ifconfig-push {ip} 255.255.255.0\n"


class TestCreatorProfile:
    def test_creator_profile_is_own_identity(self, env, mock_manager):
        response = get_profile(env.creator_token)
        assert response.status_code == 200
        mock_manager.read_client_certificate.assert_called_once_with(
            f"sf-{env.creator_id}"
        )
        rows = identity_rows(env.creator_id)
        assert len(rows) == 1
        assert rows[0].common_name == f"sf-{env.creator_id}"
        assert identity_rows(env.learner_id) == []
        assert identity_rows(env.learner_two_id) == []

    def test_creator_and_learner_get_distinct_identities(self, env, mock_manager):
        assert get_profile(env.learner_token).status_code == 200
        assert get_profile(env.creator_token).status_code == 200
        learner_ip = identity_rows(env.learner_id)[0].vpn_ip
        creator_ip = identity_rows(env.creator_id)[0].vpn_ip
        assert learner_ip != creator_ip

    def test_creator_cannot_obtain_another_users_identity(
        self, env, mock_manager
    ):
        response = get_profile(
            env.creator_token, params={"user_id": env.learner_id}
        )
        assert response.status_code == 200
        mock_manager.read_client_certificate.assert_called_with(
            f"sf-{env.creator_id}"
        )
        assert identity_rows(env.learner_id) == []

    def test_revoked_creator_identity_forbidden(self, env, mock_manager):
        assert get_profile(env.creator_token).status_code == 200
        session = SessionLocal()
        try:
            identity = session.scalar(
                select(VpnIdentity).where(VpnIdentity.user_id == env.creator_id)
            )
            identity.revoked_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()
        response = get_profile(env.creator_token)
        assert response.status_code == 403

    def test_vpn_disabled_returns_503_for_creator(self, env, key_file, monkeypatch):
        monkeypatch.setattr(settings, "vpn_enabled", False)
        monkeypatch.setattr(settings, "vpn_gateway_ssh_key_path", key_file)
        response = get_profile(env.creator_token)
        assert response.status_code == 503
        assert identity_rows(env.creator_id) == []
