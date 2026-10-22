#!/usr/bin/env python3
"""Tests for the VPN foundations (config, VpnIdentity model, VpnManager)."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings, settings
from app.database import Base
from app.models import User, VpnIdentity
from app.vpn import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    SSHErrorKind,
    SshResult,
    VpnManager,
    VpnManagerError,
    build_client_profile,
    sanitize_error,
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
def learner(db):
    user = User(
        username="vpn_learner",
        email="vpn_learner@example.com",
        password_hash="hash",
        role="learner",
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "test_id.pem"
    path.write_text("placeholder-not-a-real-key\n")
    return str(path)


@pytest.fixture
def enabled_vpn(monkeypatch, key_file):
    monkeypatch.setattr(settings, "vpn_enabled", True)
    monkeypatch.setattr(settings, "vpn_gateway_ssh_key_path", key_file)
    monkeypatch.setattr(settings, "vpn_gateway_management_host", "")


def make_identity(db, user, common_name="sf-1", vpn_ip="10.8.0.64"):
    identity = VpnIdentity(
        user_id=user.id,
        common_name=common_name,
        vpn_ip=vpn_ip,
    )
    db.add(identity)
    db.flush()
    return identity


class TestVpnConfiguration:
    def test_vpn_settings_defaults(self):
        assert Settings.model_fields["vpn_enabled"].default is False
        assert Settings.model_fields["vpn_gateway_management_host"].default == ""
        assert settings.vpn_gateway_host == "13.53.64.174"
        assert settings.vpn_gateway_user == "ubuntu"
        assert settings.vpn_gateway_ssh_key_path.endswith("CyberRange-Key.pem")
        assert settings.vpn_client_subnet == "10.8.0.0/24"
        assert settings.vpn_learner_ip_range == "10.8.0.64/26"

    def test_vpn_settings_types(self):
        assert isinstance(settings.vpn_enabled, bool)
        for name in (
            "vpn_gateway_host",
            "vpn_gateway_user",
            "vpn_gateway_ssh_key_path",
            "vpn_client_subnet",
            "vpn_learner_ip_range",
        ):
            assert isinstance(getattr(settings, name), str)

    def test_vpn_settings_overrides(self):
        custom = Settings(
            vpn_enabled=True,
            vpn_gateway_host="10.0.0.5",
            vpn_gateway_user="secforge",
            vpn_gateway_ssh_key_path="/tmp/custom.pem",
            vpn_client_subnet="10.9.0.0/24",
            vpn_learner_ip_range="10.9.0.64/26",
        )
        assert custom.vpn_enabled is True
        assert custom.vpn_gateway_host == "10.0.0.5"
        assert custom.vpn_gateway_user == "secforge"
        assert custom.vpn_gateway_ssh_key_path == "/tmp/custom.pem"
        assert custom.vpn_client_subnet == "10.9.0.0/24"
        assert custom.vpn_learner_ip_range == "10.9.0.64/26"

    def test_no_key_material_in_settings(self):
        for value in (
            settings.vpn_gateway_host,
            settings.vpn_gateway_user,
            settings.vpn_gateway_ssh_key_path,
            settings.vpn_client_subnet,
            settings.vpn_learner_ip_range,
        ):
            assert "PRIVATE KEY" not in value
            assert "BEGIN" not in value

    def test_default_command_timeout_is_bounded(self):
        assert DEFAULT_COMMAND_TIMEOUT_SECONDS > 0
        assert DEFAULT_COMMAND_TIMEOUT_SECONDS <= 120


class TestVpnIdentityModel:
    def test_vpn_identities_table_created(self, engine):
        tables = inspect(engine).get_table_names()
        assert "vpn_identities" in tables

    def test_users_have_public_profile_fields_without_vpn_credentials(self, engine):
        columns = {c["name"] for c in inspect(engine).get_columns("users")}
        assert columns == {
            "creator_access",
            "suspended",
            "id",
            "username",
            "email",
            "password_hash",
            "role",
            "created_at",
            "display_name",
            "avatar_style",
            "avatar_color",
            "bio",
            "avatar_version",
            "avatar_public",
            "bio_public",
            "password_enabled",
            "must_change_password",
            "token_version",
        }

    def test_schema_columns(self, engine):
        columns = {
            c["name"]: c for c in inspect(engine).get_columns("vpn_identities")
        }
        assert set(columns) == {
            "id",
            "user_id",
            "common_name",
            "vpn_ip",
            "created_at",
            "updated_at",
            "revoked_at",
        }
        assert columns["user_id"]["nullable"] is False
        assert columns["common_name"]["nullable"] is False
        assert columns["vpn_ip"]["nullable"] is False
        assert columns["revoked_at"]["nullable"] is True

    def test_unique_constraints(self, engine):
        uniques = {
            tuple(c["column_names"])
            for c in inspect(engine).get_unique_constraints("vpn_identities")
        }
        assert ("user_id",) in uniques
        assert ("common_name",) in uniques
        assert ("vpn_ip",) in uniques

    def test_foreign_key_to_users(self, engine):
        fks = inspect(engine).get_foreign_keys("vpn_identities")
        assert any(
            fk["referred_table"] == "users"
            and fk["referred_columns"] == ["id"]
            for fk in fks
        )

    def test_create_identity(self, db, learner):
        identity = make_identity(db, learner)
        assert identity.id is not None
        assert identity.user_id == learner.id
        assert identity.common_name == "sf-1"
        assert identity.vpn_ip == "10.8.0.64"
        assert identity.revoked_at is None
        assert identity.created_at is not None
        assert identity.updated_at is not None

    def test_duplicate_user_id_rejected(self, db, learner):
        make_identity(db, learner)
        duplicate = VpnIdentity(
            user_id=learner.id,
            common_name="sf-2",
            vpn_ip="10.8.0.65",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_duplicate_common_name_rejected(self, db, learner, engine):
        other = User(
            username="other_learner",
            email="other@example.com",
            password_hash="hash",
            role="learner",
        )
        db.add(other)
        db.flush()
        make_identity(db, learner, common_name="sf-1", vpn_ip="10.8.0.64")
        duplicate = VpnIdentity(
            user_id=other.id,
            common_name="sf-1",
            vpn_ip="10.8.0.65",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_duplicate_vpn_ip_rejected(self, db, learner, engine):
        other = User(
            username="other_learner_2",
            email="other2@example.com",
            password_hash="hash",
            role="learner",
        )
        db.add(other)
        db.flush()
        make_identity(db, learner, common_name="sf-1", vpn_ip="10.8.0.64")
        duplicate = VpnIdentity(
            user_id=other.id,
            common_name="sf-2",
            vpn_ip="10.8.0.64",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_relationship_identity_to_user(self, db, learner):
        identity = make_identity(db, learner)
        assert identity.user is not None
        assert identity.user.id == learner.id
        assert identity.user.username == "vpn_learner"

    def test_relationship_user_to_identity(self, db, learner):
        identity = make_identity(db, learner)
        fetched = db.scalar(select(User).where(User.id == learner.id))
        assert fetched.vpn_identity is not None
        assert fetched.vpn_identity.id == identity.id

    def test_relationship_null_for_user_without_identity(self, db, learner):
        fetched = db.scalar(select(User).where(User.id == learner.id))
        assert fetched.vpn_identity is None

    def test_revoked_at_can_be_set(self, db, learner):
        from datetime import datetime

        identity = make_identity(db, learner)
        identity.revoked_at = datetime.utcnow()
        db.flush()
        db.refresh(identity)
        assert identity.revoked_at is not None


class TestSshCommandConstruction:
    def test_build_ssh_command_structure(self, enabled_vpn):
        manager = VpnManager()
        argv = manager.build_ssh_command(["echo", "hello"])
        assert argv[0] == "ssh"
        assert argv[argv.index("-F") + 1] == "/dev/null"
        assert "-i" in argv
        assert argv[argv.index("-i") + 1] == settings.vpn_gateway_ssh_key_path
        assert "StrictHostKeyChecking=yes" in argv
        assert "StrictHostKeyChecking=no" not in argv
        assert "BatchMode=yes" in argv
        assert "IdentitiesOnly=yes" in argv
        assert "PasswordAuthentication=no" in argv
        assert f"{settings.vpn_gateway_user}@{settings.vpn_gateway_host}" in argv
        assert argv[-1] == "echo hello"

    def test_build_ssh_command_does_not_execute(self, enabled_vpn):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            manager.build_ssh_command(["echo", "hello"])
        mock_run.assert_not_called()

    def test_management_host_is_separate_from_public_profile_endpoint(
        self, enabled_vpn, monkeypatch
    ):
        monkeypatch.setattr(settings, "vpn_gateway_host", "public-gateway.invalid")
        monkeypatch.setattr(settings, "vpn_gateway_management_host", "management-gateway.invalid")
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            manager.run_command(["true"])
        argv = run.call_args.args[0]
        assert f"{settings.vpn_gateway_user}@management-gateway.invalid" in argv
        assert f"{settings.vpn_gateway_user}@public-gateway.invalid" not in argv
        assert settings.vpn_gateway_host == "public-gateway.invalid"
        assert "StrictHostKeyChecking=yes" in argv

    def test_builder_target_stays_explicit_with_management_host_configured(
        self, enabled_vpn, monkeypatch
    ):
        monkeypatch.setattr(settings, "vpn_gateway_management_host", "management-gateway.invalid")
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            manager.run_builder_command("builder.invalid", ["true"])
        argv = run.call_args.args[0]
        assert f"{settings.builder_ssh_user}@builder.invalid" in argv
        assert f"{settings.vpn_gateway_user}@management-gateway.invalid" not in argv
        assert argv[argv.index("-F") + 1] == "/dev/null"
        assert "StrictHostKeyChecking=yes" in argv
        assert "BatchMode=yes" in argv

    def test_management_host_does_not_change_generated_vpn_profile(
        self, enabled_vpn, monkeypatch
    ):
        monkeypatch.setattr(settings, "vpn_gateway_host", "public-gateway.invalid")
        monkeypatch.setattr(settings, "vpn_gateway_management_host", "management-gateway.invalid")
        manager = MagicMock()
        # These are format placeholders, not usable certificates or keys.
        manager.read_ca_certificate.return_value = "-----BEGIN CERTIFICATE-----\noffline-ca"
        manager.read_client_certificate.return_value = "-----BEGIN CERTIFICATE-----\noffline-cert"
        manager.read_client_key.return_value = "-----BEGIN PRIVATE KEY-----\noffline-placeholder"
        manager.read_tls_crypt_key.return_value = "-----BEGIN OpenVPN Static key V1-----\noffline-placeholder"
        profile = build_client_profile(manager, "sf-1", "10.8.0.65")
        assert f"remote public-gateway.invalid {settings.vpn_gateway_port}\n" in profile
        assert "management-gateway.invalid" not in profile

    def test_command_timeout_is_applied(self, enabled_vpn):
        manager = VpnManager(timeout=7.5)
        with patch("app.vpn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ok", stderr=""
            )
            manager.run_command(["echo", "ok"])
        assert mock_run.call_args.kwargs["timeout"] == 7.5


class TestVpnManagerExecution:
    def test_successful_command(self, enabled_vpn):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="issued\n",
                stderr="",
            )
            result = manager.run_command(["echo", "issued"])

        assert isinstance(result, SshResult)
        assert result.stdout == "issued\n"
        assert result.returncode == 0

        argv = mock_run.call_args.args[0]
        assert isinstance(argv, list)
        assert mock_run.call_args.kwargs.get("shell") in (None, False)
        assert mock_run.call_args.kwargs["check"] is False
        assert mock_run.call_args.kwargs["capture_output"] is True

    def test_ssh_timeout_raises(self, enabled_vpn):
        manager = VpnManager(timeout=1.0)
        with patch("app.vpn.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="ssh", timeout=1.0
            )
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command(["echo", "never"])
        assert "timed out" in str(exc_info.value)

    def test_nonzero_exit_raises_sanitized(self, enabled_vpn):
        manager = VpnManager()
        dirty_stderr = (
            "permission denied\n"
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            "-----END PRIVATE KEY-----\n"
            "password=hunter2\n"
        )
        with patch("app.vpn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=255,
                stdout="",
                stderr=dirty_stderr,
            )
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command(["echo", "nope"])

        message = str(exc_info.value)
        assert exc_info.value.returncode == 255
        assert "exit code 255" in message
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in message
        assert "PRIVATE KEY" not in message
        assert "hunter2" not in message
        assert "REDACTED" in message

    def test_zero_exit_returns_result_with_stderr(self, enabled_vpn):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="data",
                stderr="warning",
            )
            result = manager.run_command(["echo", "data"])
        assert result.stderr == "warning"
        assert result.stdout == "data"

    def test_disabled_manager_does_not_execute(self, monkeypatch, key_file):
        monkeypatch.setattr(settings, "vpn_enabled", False)
        monkeypatch.setattr(settings, "vpn_gateway_ssh_key_path", key_file)
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command(["echo", "nope"])
        assert "disabled" in str(exc_info.value)
        mock_run.assert_not_called()

    def test_incomplete_config_rejected(self, enabled_vpn, monkeypatch):
        monkeypatch.setattr(settings, "vpn_gateway_host", "")
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command(["echo", "nope"])
        assert "incomplete" in str(exc_info.value)
        mock_run.assert_not_called()

    def test_missing_identity_file_rejected(self, enabled_vpn, monkeypatch):
        monkeypatch.setattr(
            settings, "vpn_gateway_ssh_key_path", "/nonexistent/id.pem"
        )
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command(["echo", "nope"])
        assert "identity file not found" in str(exc_info.value)
        mock_run.assert_not_called()

    def test_whitespace_in_config_rejected(self, enabled_vpn, monkeypatch):
        monkeypatch.setattr(settings, "vpn_gateway_user", "ubuntu root")
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError):
                manager.run_command(["echo", "nope"])
        mock_run.assert_not_called()

    def test_oserror_raises_sanitized(self, enabled_vpn):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ssh binary missing")
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command(["echo", "nope"])
        assert "could not be started" in str(exc_info.value)


class TestSshErrorClassification:
    """``VpnManagerError.kind`` distinguishes the failure categories."""

    def _fail_with(self, enabled_vpn, *, returncode=None, stderr="", side_effect=None):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            if side_effect is not None:
                mock_run.side_effect = side_effect
            else:
                mock_run.return_value = MagicMock(
                    returncode=returncode, stdout="", stderr=stderr
                )
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command(["echo", "x"])
        return exc_info.value

    def test_command_timeout_kind(self, enabled_vpn):
        error = self._fail_with(
            enabled_vpn,
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=1.0),
        )
        assert error.kind == SSHErrorKind.COMMAND_TIMEOUT.value

    def test_connection_refused_kind(self, enabled_vpn):
        error = self._fail_with(
            enabled_vpn,
            returncode=255,
            stderr="ssh: connect to host 10.0.1.39 port 22: Connection refused",
        )
        assert error.kind == SSHErrorKind.CONNECTION.value

    def test_host_key_failure_kind(self, enabled_vpn):
        error = self._fail_with(
            enabled_vpn, returncode=255, stderr="Host key verification failed."
        )
        assert error.kind == SSHErrorKind.HOST_KEY.value

    def test_authentication_failure_kind(self, enabled_vpn):
        error = self._fail_with(
            enabled_vpn,
            returncode=255,
            stderr="ubuntu@10.0.1.39: Permission denied (publickey).",
        )
        assert error.kind == SSHErrorKind.AUTHENTICATION.value

    def test_remote_command_failure_kind(self, enabled_vpn):
        error = self._fail_with(
            enabled_vpn, returncode=1, stderr="sudo: a password is required"
        )
        assert error.kind == SSHErrorKind.REMOTE_COMMAND.value

    def test_oserror_classified_as_connection(self, enabled_vpn):
        error = self._fail_with(
            enabled_vpn, side_effect=FileNotFoundError("ssh binary missing")
        )
        assert error.kind == SSHErrorKind.CONNECTION.value

    def test_default_kind_is_unknown(self, monkeypatch, key_file):
        monkeypatch.setattr(settings, "vpn_enabled", False)
        manager = VpnManager()
        with pytest.raises(VpnManagerError) as exc_info:
            manager.run_command(["echo", "x"])
        assert exc_info.value.kind == SSHErrorKind.UNKNOWN.value


class TestShellStringRejection:
    def test_shell_string_rejected(self, enabled_vpn):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError) as exc_info:
                manager.run_command("id; cat /etc/shadow")
        assert "shell string" in str(exc_info.value)
        mock_run.assert_not_called()

    def test_bytes_rejected(self, enabled_vpn):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError):
                manager.run_command(b"echo pwned")
        mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "bad_token",
        [
            "id;reboot",
            "id&&reboot",
            "$(reboot)",
            "`reboot`",
            "a|b",
            "a>b",
            "a\nb",
            "a b",
            "a$PATH",
            "a{b}",
            "*",
            "~/x",
        ],
    )
    def test_metacharacter_tokens_rejected(self, enabled_vpn, bad_token):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError):
                manager.run_command(["echo", bad_token])
        mock_run.assert_not_called()

    def test_empty_command_rejected(self, enabled_vpn):
        manager = VpnManager()
        with patch("app.vpn.subprocess.run") as mock_run:
            with pytest.raises(VpnManagerError):
                manager.run_command([])
        mock_run.assert_not_called()

    def test_safe_tokens_accepted(self, enabled_vpn):
        manager = VpnManager()
        tokens = [
            "easyrsa",
            "--batch",
            "build-client-full",
            "sf-42",
            "10.8.0.64",
            "CN=sf-42",
            "/etc/openvpn/pki",
        ]
        argv = manager.build_ssh_command(tokens)
        assert argv[-1] == " ".join(tokens)


class TestErrorSanitization:
    def test_pem_blocks_removed(self):
        dirty = (
            "failed\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWy\n"
            "-----END RSA PRIVATE KEY-----"
        )
        cleaned = sanitize_error(dirty)
        assert "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn" not in cleaned
        assert "PRIVATE KEY" not in cleaned
        assert "REDACTED" in cleaned

    def test_secret_assignments_redacted(self):
        cleaned = sanitize_error("login failed password=hunter2 token=abc123")
        assert "hunter2" not in cleaned
        assert "abc123" not in cleaned
        assert "password=[REDACTED]" in cleaned

    def test_long_base64_redacted(self):
        blob = "A" * 200
        cleaned = sanitize_error(f"payload={blob} end")
        assert blob not in cleaned
        assert "REDACTED" in cleaned

    def test_output_is_bounded(self):
        cleaned = sanitize_error("error detail " * 600)
        assert len(cleaned) <= 520
        assert cleaned.endswith("...[truncated]")

    def test_newlines_collapsed(self):
        cleaned = sanitize_error("line1\nline2\r\nline3")
        assert "\n" not in cleaned
        assert cleaned == "line1 line2 line3"

    def test_empty_input(self):
        assert sanitize_error("") == ""
        assert sanitize_error(None) == ""

    def test_exception_message_is_sanitized_by_construction(self):
        dirty = "-----BEGIN PRIVATE KEY-----\nsecretblob\n-----END PRIVATE KEY-----"
        error = VpnManagerError(sanitize_error(dirty), returncode=1)
        assert "secretblob" not in str(error)
        assert error.returncode == 1
