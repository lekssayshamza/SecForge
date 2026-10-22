"""Controlled access to the shared SecForge VPN gateway over system ``ssh``.

This module provides a subprocess wrapper around the system ``ssh`` binary
plus the gateway operations needed for learner profile issuance: client
certificate issuance, certificate/key/CA/tls-crypt reads, CCD writes,
client profile rendering and dynamic forward-rule synchronization for
learner-to-lab authorization.

Security invariants:
- Commands are run as an argv list via ``subprocess.run`` with
  ``shell=False`` semantics; no shell string is ever constructed locally.
- Remote commands are assembled exclusively from strictly validated tokens
  that cannot carry shell metacharacters; caller-supplied shell strings are
  rejected before any process is spawned.
- Host keys are verified strictly (``StrictHostKeyChecking=yes``) and
  authentication is non-interactive (``BatchMode=yes``); host verification
  is never silently disabled.
- Every command has a timeout; failures raise ``VpnManagerError`` whose
  message is sanitized (PEM blocks, long base64 blobs and secret-like
  assignments are redacted, output is bounded).
- Private key material and ``.ovpn`` contents are never accepted as
  arguments and are never logged by this module. Key material is only ever
  read from the gateway and returned to the caller as strings.
- Client common names and VPN addresses are strictly validated before they
  are interpolated into any remote command or filesystem path.
"""

from __future__ import annotations

import ipaddress
import base64
import hashlib
import tempfile
from pathlib import Path
import os
import re
import select
import subprocess
import time
from contextlib import contextmanager
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.config import settings

__all__ = [
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "EASYRSA_BATCH_ARGS",
    "EASYRSA_PATH",
    "EASYRSA_VARS_PATH",
    "EXCLUDED_VPN_IPS",
    "SSHErrorKind",
    "SshResult",
    "VpnManager",
    "VpnManagerError",
    "build_client_profile",
    "common_name_for_user",
    "iter_candidate_ips",
    "render_client_profile",
    "sanitize_error",
    "validate_common_name",
    "validate_vpn_ip",
]

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0
SSH_CONNECT_TIMEOUT_SECONDS = 10
MAX_SANITIZED_ERROR_CHARS = 500

# Keep the privileged session open while the root filesystem is frozen. A new
# SSH login can block on guest logging/PAM writes to that frozen filesystem.
# EOF, a signal, or a lost backend's bounded deadline releases the filesystem.
BUILDER_FREEZE_GUARD_SECONDS = 180
BUILDER_FREEZE_SCRIPT = (
    "trap 'fsfreeze -u / >/dev/null 2>&1 || true' EXIT; "
    "trap 'exit 1' HUP INT TERM; "
    "fsfreeze -f / || exit 1; printf 'SF_FROZEN\\n'; "
    f"if IFS= read -r -t {BUILDER_FREEZE_GUARD_SECONDS} release "
    "&& [ \"$release\" = UNFREEZE ]; then "
    "fsfreeze -u / || exit 1; trap - EXIT; "
    "printf 'SF_UNFROZEN\\n'; else exit 1; fi\n"
)

EASYRSA_PATH = "/etc/openvpn/easy-rsa/easyrsa"
EASYRSA_VARS_PATH = "/etc/openvpn/easy-rsa/vars"
PKI_PATH = "/etc/openvpn/pki"
CCD_DIR = "/etc/openvpn/server/ccd"
TA_KEY_PATH = "/etc/openvpn/server/ta.key"
EASYRSA_BATCH_ARGS: tuple[str, ...] = (
    "sudo",
    "-n",
    EASYRSA_PATH,
    "--batch",
    f"--vars={EASYRSA_VARS_PATH}",
)
EXCLUDED_VPN_IPS = frozenset({"10.8.0.20"})

_SAFE_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_./:=@%+-]+\Z")
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*-----.*?-----END [A-Z0-9 ]*-----",
    re.DOTALL,
)
_LONG_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization"
    r"|private[_ -]?key)\s*[=:]\s*\S+",
)


class SSHErrorKind(str, Enum):
    """Machine-readable cause of a failed remote SSH command.

    Callers that persist or surface the failure (for example the builder
    template-capture path) can use this to distinguish a transport or
    authentication problem from a genuine remote-command failure without
    parsing the sanitized message text.
    """

    CONNECTION = "connection"
    HOST_KEY = "host_key"
    AUTHENTICATION = "authentication"
    COMMAND_TIMEOUT = "command_timeout"
    REMOTE_COMMAND = "remote_command"
    UNKNOWN = "unknown"


class VpnManagerError(Exception):
    """Failure of a VPN gateway operation.

    The exception message is always sanitized before it is attached, so it
    is safe to return to API callers or write to logs. ``kind`` classifies
    the failure (see ``SSHErrorKind``) and defaults to ``unknown`` for
    validation and configuration errors raised before a command is run.
    """

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        kind: str = SSHErrorKind.UNKNOWN.value,
    ):
        super().__init__(message)
        self.returncode = returncode
        self.kind = kind


@dataclass(frozen=True)
class SshResult:
    """Outcome of a successful remote command."""

    stdout: str
    stderr: str
    returncode: int


def sanitize_error(text: object) -> str:
    """Redact key material and secret-like values, then bound the output."""
    if text is None:
        return ""
    cleaned = str(text)
    if not cleaned:
        return ""
    cleaned = _PEM_BLOCK_RE.sub("[REDACTED-PEM]", cleaned)
    cleaned = _SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _LONG_BASE64_RE.sub("[REDACTED]", cleaned)
    cleaned = cleaned.replace("\x00", " ")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_SANITIZED_ERROR_CHARS:
        cleaned = cleaned[:MAX_SANITIZED_ERROR_CHARS] + "...[truncated]"
    return cleaned


# stderr markers used to classify a nonzero ``ssh`` exit. Host-key and
# authentication are matched first because a rejected connection is not a
# remote-command failure. Markers are matched case-insensitively.
_HOST_KEY_ERROR_MARKERS = ("host key verification failed",)
_AUTHENTICATION_ERROR_MARKERS = (
    "permission denied",
    "too many authentication failures",
    "no supported authentication methods",
    "authentication failed",
)
_CONNECTION_ERROR_MARKERS = (
    "connection timed out",
    "connection refused",
    "connection closed",
    "connection reset",
    "no route to host",
    "network is unreachable",
    "operation timed out",
    "broken pipe",
)


def _classify_ssh_failure(stderr: str) -> str:
    """Classify a nonzero-exit SSH failure by inspecting stderr."""
    text = (stderr or "").lower()
    for kind, markers in (
        (SSHErrorKind.HOST_KEY, _HOST_KEY_ERROR_MARKERS),
        (SSHErrorKind.AUTHENTICATION, _AUTHENTICATION_ERROR_MARKERS),
        (SSHErrorKind.CONNECTION, _CONNECTION_ERROR_MARKERS),
    ):
        if any(marker in text for marker in markers):
            return kind.value
    return SSHErrorKind.REMOTE_COMMAND.value


_COMMON_NAME_RE = re.compile(r"\Asf-[1-9][0-9]*\Z")


def _learner_network() -> "ipaddress.IPv4Network":
    try:
        network = ipaddress.ip_network(settings.vpn_learner_ip_range, strict=True)
    except ValueError:
        raise VpnManagerError("Configured learner IP range is invalid") from None
    if network.version != 4:
        raise VpnManagerError("Configured learner IP range is invalid")
    return network


def validate_common_name(common_name: str) -> str:
    """Validate a learner client common name of the form ``sf-<user_id>``."""
    if not isinstance(common_name, str) or not _COMMON_NAME_RE.match(common_name):
        raise VpnManagerError("VPN common name is invalid")
    return common_name


def common_name_for_user(user_id: int) -> str:
    """Derive the strictly validated common name for a learner user id."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise VpnManagerError("Learner id is invalid for VPN identity")
    return validate_common_name(f"sf-{user_id}")


def validate_vpn_ip(vpn_ip: str) -> str:
    """Validate a learner VPN address inside the configured learner range."""
    try:
        address = ipaddress.ip_address(vpn_ip)
    except ValueError:
        raise VpnManagerError("VPN address is invalid") from None
    if address.version != 4:
        raise VpnManagerError("VPN address is invalid")
    network = _learner_network()
    if address in (network.network_address, network.broadcast_address):
        raise VpnManagerError("VPN address is reserved")
    if address not in network:
        raise VpnManagerError("VPN address is outside the learner range")
    if str(address) in EXCLUDED_VPN_IPS:
        raise VpnManagerError("VPN address is reserved")
    return str(address)


def iter_candidate_ips(used_ips: Iterable[str]) -> Iterator[str]:
    """Yield free learner addresses from the configured range, in order."""
    network = _learner_network()
    taken = set(used_ips)
    taken.update(EXCLUDED_VPN_IPS)
    for host in network.hosts():
        ip = str(host)
        if ip not in taken:
            yield ip


def render_client_profile(
    *,
    host: str,
    port: int,
    ca_certificate: str,
    client_certificate: str,
    client_key: str,
    tls_crypt_key: str,
) -> str:
    """Render a client ``.ovpn`` profile matching the working test client."""
    if not host or any(ch.isspace() for ch in host) or "\n" in host:
        raise VpnManagerError("VPN gateway host is invalid")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise VpnManagerError("VPN gateway port is invalid")
    for label, material in (
        ("CA certificate", ca_certificate),
        ("client certificate", client_certificate),
        ("client private key", client_key),
        ("tls-crypt key", tls_crypt_key),
    ):
        if not isinstance(material, str) or "-----BEGIN" not in material:
            raise VpnManagerError(f"VPN gateway returned invalid {label} material")
    return (
        "client\n"
        "dev tun\n"
        "proto udp\n"
        f"remote {host} {port}\n"
        "resolv-retry infinite\n"
        "nobind\n"
        "persist-key\n"
        "persist-tun\n"
        "remote-cert-tls server\n"
        "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305\n"
        "auth SHA256\n"
        "tls-version-min 1.2\n"
        "verb 3\n"
        "\n"
        f"<ca>\n{ca_certificate.strip()}\n</ca>\n"
        "\n"
        f"<cert>\n{client_certificate.strip()}\n</cert>\n"
        "\n"
        f"<key>\n{client_key.strip()}\n</key>\n"
        "\n"
        f"<tls-crypt>\n{tls_crypt_key.strip()}\n</tls-crypt>\n"
    )


def build_client_profile(
    manager: "VpnManager",
    common_name: str,
    vpn_ip: str,
) -> str:
    """Validate identity attributes, read gateway material, render profile."""
    cn = validate_common_name(common_name)
    ip = validate_vpn_ip(vpn_ip)
    ca_certificate = manager.read_ca_certificate()
    client_certificate = manager.read_client_certificate(cn)
    client_key = manager.read_client_key(cn)
    tls_crypt_key = manager.read_tls_crypt_key()
    return render_client_profile(
        host=settings.vpn_gateway_host,
        port=settings.vpn_gateway_port,
        ca_certificate=ca_certificate,
        client_certificate=client_certificate,
        client_key=client_key,
        tls_crypt_key=tls_crypt_key,
    )


class VpnManager:
    """Controlled access to the shared VPN gateway over the system ``ssh``."""

    def __init__(self, timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS):
        self.timeout = timeout
        self._builder_known_hosts: dict[str, str] = {}

    @contextmanager
    def verified_builder_host(self, host: str, console_output: str):
        """Pin a builder key against its authenticated EC2 boot-console record.

        A scan alone is never trusted. The private file exists only for this
        operation, so recycled private addresses cannot reuse an old pin.
        Gateway SSH and the user's global known-hosts file are unaffected.
        """
        try:
            address = ipaddress.ip_address(host)
            if address.version != 4 or not address.is_private or host in self._builder_known_hosts:
                raise ValueError
            scan = subprocess.run(
                ["ssh-keyscan", "-T", "8", "-t", "ed25519", host],
                capture_output=True, text=True, timeout=10,
            )
            lines = [line.split() for line in scan.stdout.splitlines()
                     if line and not line.startswith("#")]
            if scan.returncode or len(lines) != 1:
                raise ValueError
            target, algorithm, encoded = lines[0]
            if target != host or algorithm != "ssh-ed25519":
                raise ValueError
            key = base64.b64decode(encoded, validate=True)
            fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
            # Botocore normally decodes console output; also accept encoded
            # output from offline adapters. Require a whole fingerprint token.
            outputs = [console_output]
            try:
                outputs.append(base64.b64decode(console_output, validate=True).decode())
            except (ValueError, UnicodeError):
                pass
            if not any(fingerprint in re.findall(r"SHA256:[A-Za-z0-9+/]+", output)
                       for output in outputs):
                raise ValueError
        except (ValueError, TypeError, OSError, subprocess.TimeoutExpired):
            raise VpnManagerError("Builder SSH host key could not be verified against its EC2 console") from None

        with tempfile.TemporaryDirectory(prefix="secforge-builder-ssh-") as directory:
            known = Path(directory) / "known_hosts"
            known.write_text(f"{host} ssh-ed25519 {encoded}\n")
            known.chmod(0o600)
            self._builder_known_hosts[host] = str(known)
            try:
                yield
            finally:
                self._builder_known_hosts.pop(host, None)

    @property
    def enabled(self) -> bool:
        return bool(settings.vpn_enabled)

    def build_ssh_command(
        self,
        remote_args: Sequence[str],
        *,
        host: str | None = None,
        user: str | None = None,
        key_path: str | None = None,
    ) -> list[str]:
        """Build the local argv for an ssh invocation. Never executes.

        ``host``/``user``/``key_path`` default to the shared VPN gateway; the
        builder remote-command path passes an explicit target while reusing
        this exact hardening.
        """
        tokens = self._validate_remote_args(remote_args)
        host = host or settings.vpn_gateway_management_host or settings.vpn_gateway_host
        user = user or settings.vpn_gateway_user
        key_path = key_path or settings.vpn_gateway_ssh_key_path
        if not host or not user or not key_path:
            raise VpnManagerError("VPN gateway configuration is incomplete")
        verified_options = []
        if host in self._builder_known_hosts:
            verified_options = [
                "-o", "UserKnownHostsFile=" + self._builder_known_hosts[host],
                "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "HostKeyAlgorithms=ssh-ed25519",
                "-o", "UpdateHostKeys=no",
            ]
        return [
            "ssh",
            # Use only the application's explicit connection settings. Ambient
            # SSH config can redirect targets or fail local ownership checks.
            "-F",
            "/dev/null",
            "-i",
            key_path,
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
            *verified_options,
            "--",
            f"{user}@{host}",
            " ".join(tokens),
        ]

    def run_command(
        self,
        remote_args: Sequence[str],
        timeout: float | None = None,
        *,
        stdin_data: str | None = None,
        host: str | None = None,
        user: str | None = None,
        key_path: str | None = None,
    ) -> SshResult:
        """Run a validated remote command on the gateway and return its output.

        An explicit ``host``/``user``/``key_path`` retargets the same hardened
        transport (used for builder remote commands); defaults are the gateway.
        """
        if not self.enabled:
            raise VpnManagerError("VPN gateway management is disabled")
        self._validate_connection_settings(host=host, user=user, key_path=key_path)
        argv = self.build_ssh_command(
            remote_args, host=host, user=user, key_path=key_path
        )
        effective_timeout = self.timeout if timeout is None else timeout

        run_kwargs: dict = {
            "capture_output": True,
            "text": True,
            "timeout": effective_timeout,
            "check": False,
        }
        if stdin_data is None:
            run_kwargs["stdin"] = subprocess.DEVNULL
        else:
            if not isinstance(stdin_data, str):
                raise VpnManagerError("Command input must be text")
            run_kwargs["input"] = stdin_data

        try:
            result = subprocess.run(argv, **run_kwargs)
        except subprocess.TimeoutExpired:
            raise VpnManagerError(
                f"SSH command timed out after {effective_timeout:g} seconds",
                kind=SSHErrorKind.COMMAND_TIMEOUT.value,
            ) from None
        except OSError as exc:
            raise VpnManagerError(
                f"SSH transport could not be started: {sanitize_error(exc)}",
                kind=SSHErrorKind.CONNECTION.value,
            ) from None

        if result.returncode != 0:
            detail = sanitize_error(result.stderr or result.stdout)
            message = f"SSH command failed with exit code {result.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise VpnManagerError(
                message,
                returncode=result.returncode,
                kind=_classify_ssh_failure(result.stderr or result.stdout),
            )

        return SshResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )

    def run_builder_command(
        self,
        builder_host: str,
        remote_args: Sequence[str],
        timeout: float | None = None,
        *,
        stdin_data: str | None = None,
    ) -> SshResult:
        """Run a validated command on a builder host over the same hardened SSH.

        Reuses the exact gateway transport (strict host-key checking,
        ``BatchMode``, ``IdentitiesOnly``, bounded ``ConnectTimeout``) with an
        explicit target, so no new SSH implementation is introduced.
        """
        host = (builder_host or "").strip()
        if not host or any(ch.isspace() for ch in host):
            raise VpnManagerError("Builder host is invalid")
        key_path = settings.builder_ssh_key_path or settings.vpn_gateway_ssh_key_path
        return self.run_command(
            remote_args,
            timeout=timeout,
            host=host,
            user=settings.builder_ssh_user,
            key_path=key_path,
            **({'stdin_data': stdin_data} if stdin_data is not None else {}),
        )

    @contextmanager
    def frozen_builder(self, builder_host: str, timeout: float = 30):
        """Quiesce and release through one already-authenticated root session.

        The fixed program travels on stdin, so the remote-argument guard and
        strict SSH authentication remain intact. Its EXIT trap and timed read
        also release the filesystem when the caller crashes or disconnects.
        """
        argv = self.build_ssh_command(
            ["sudo", "-n", "bash", "-s"], host=builder_host,
            user=settings.builder_ssh_user,
            key_path=settings.builder_ssh_key_path or settings.vpn_gateway_ssh_key_path,
        )
        process = None
        frozen = False

        def acknowledge(marker):
            deadline = time.monotonic() + timeout
            output = b""
            streams = [process.stdout, process.stderr]
            while streams and time.monotonic() < deadline:
                ready, _, _ = select.select(streams, [], [], max(0, deadline - time.monotonic()))
                for stream in ready:
                    chunk = os.read(stream.fileno(), 4096)
                    if not chunk:
                        streams.remove(stream)
                    elif stream is process.stdout:
                        output += chunk
                        if marker in output.splitlines():
                            return
                        if len(output) > 8192:
                            raise VpnManagerError("Unexpected builder quiesce response")
            raise VpnManagerError(
                "Builder filesystem session did not acknowledge "
                + ("freeze" if marker == b"SF_FROZEN" else "unfreeze"),
                kind=SSHErrorKind.COMMAND_TIMEOUT.value,
            )

        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            process.stdin.write(BUILDER_FREEZE_SCRIPT.encode())
            process.stdin.flush()
            acknowledge(b"SF_FROZEN")
            frozen = True
            try:
                yield
            finally:
                if frozen:
                    if process.poll() is not None or process.stdin.closed:
                        raise VpnManagerError("Builder filesystem session closed before release")
                    process.stdin.write(b"UNFREEZE\n")
                    process.stdin.flush()
                    acknowledge(b"SF_UNFROZEN")
                    process.stdin.close()
                    process.wait(timeout=timeout)
                    if process.returncode:
                        raise VpnManagerError("Builder filesystem release failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VpnManagerError("Builder filesystem session failed",
                                  kind=SSHErrorKind.CONNECTION.value) from exc
        finally:
            if process is not None:
                # Closing the transport makes the guest read see EOF; its trap
                # performs release. The independent guest deadline is retained
                # even when a network failure prevents EOF from arriving.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def issue_client_certificate(self, common_name: str) -> None:
        """Issue or renew a client certificate (never prints key material)."""
        cn = validate_common_name(common_name)
        self.run_command(
            [*EASYRSA_BATCH_ARGS, "build-client-full", cn, "nopass"]
        )

    def read_client_certificate(self, common_name: str) -> str:
        cn = validate_common_name(common_name)
        return self.run_command(
            ["sudo", "-n", "cat", f"{PKI_PATH}/issued/{cn}.crt"]
        ).stdout

    def read_client_key(self, common_name: str) -> str:
        cn = validate_common_name(common_name)
        return self.run_command(
            ["sudo", "-n", "cat", f"{PKI_PATH}/private/{cn}.key"]
        ).stdout

    def read_ca_certificate(self) -> str:
        return self.run_command(["sudo", "-n", "cat", f"{PKI_PATH}/ca.crt"]).stdout

    def read_tls_crypt_key(self) -> str:
        return self.run_command(["sudo", "-n", "cat", TA_KEY_PATH]).stdout

    def write_client_ccd(self, common_name: str, vpn_ip: str) -> None:
        """Write a learner CCD file assigning a static in-range address."""
        cn = validate_common_name(common_name)
        ip = validate_vpn_ip(vpn_ip)
        self.run_command(
            ["sudo", "-n", "tee", f"{CCD_DIR}/{cn}"],
            stdin_data=f"ifconfig-push {ip} 255.255.255.0\n",
        )

    def list_forward_chain_rules(self) -> str:
        """Read the live ``inet filter forward`` chain including rule handles."""
        return self.run_command(
            ["sudo", "-n", "nft", "-a", "list", "chain", "inet", "filter", "forward"]
        ).stdout

    def sync_forward_rules(
        self,
        pairs: Iterable[tuple[str, str]],
        existing_rule_handle: int | None = None,
        *,
        builder_ips: Iterable[str] | None = None,
        existing_egress_handle: int | None = None,
    ) -> None:
        """Atomically replace the dedicated secforge-vpn set and filter rule.

        The rendered transaction recreates ``table inet secforge-vpn`` (the
        named ``lab_pairs`` set) and manages a single marked accept rule
        inside the existing ``inet filter forward`` chain — deleted by
        handle, then re-added with the authorized pairs embedded inline
        (nftables cannot reference a set across tables) — so authorized
        traffic is accepted by the same base chain whose policy would
        otherwise drop it. It is piped to ``sudo -n nft -f -`` on stdin; no
        shell is involved. When explicitly configured, the same transaction
        also replaces the owned builder-egress rule/table. Existing input,
        established-connection and default-policy rules remain untouched.
        """
        from app.vpn_authz import render_nft_transaction

        transaction = render_nft_transaction(
            pairs, existing_rule_handle=existing_rule_handle,
            builder_ips=builder_ips, existing_egress_handle=existing_egress_handle,
        )
        self.run_command(["sudo", "-n", "nft", "-f", "-"], stdin_data=transaction)

    def _validate_connection_settings(
        self,
        *,
        host: str | None = None,
        user: str | None = None,
        key_path: str | None = None,
    ) -> None:
        host = host or settings.vpn_gateway_management_host or settings.vpn_gateway_host
        user = user or settings.vpn_gateway_user
        key_path = key_path or settings.vpn_gateway_ssh_key_path
        if not host or not user or not key_path:
            raise VpnManagerError("VPN gateway configuration is incomplete")
        for value in (host, user, key_path):
            if any(ch.isspace() for ch in value):
                raise VpnManagerError(
                    "VPN gateway configuration contains invalid characters"
                )
        if "@" in user:
            raise VpnManagerError(
                "VPN gateway configuration contains invalid characters"
            )
        if not os.path.isfile(key_path):
            raise VpnManagerError(
                f"VPN gateway SSH identity file not found: {key_path}"
            )

    @staticmethod
    def _validate_remote_args(remote_args: Sequence[str]) -> list[str]:
        if isinstance(remote_args, (str, bytes)):
            raise VpnManagerError(
                "Remote commands must be an argument list, not a shell string"
            )
        try:
            tokens = list(remote_args)
        except TypeError:
            raise VpnManagerError(
                "Remote commands must be an argument list, not a shell string"
            ) from None
        if not tokens:
            raise VpnManagerError("Empty remote command rejected")
        for token in tokens:
            if not isinstance(token, str) or not _SAFE_TOKEN_RE.match(token):
                raise VpnManagerError("Unsafe remote command token rejected")
        return tokens
