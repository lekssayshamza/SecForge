"""Host-side /32 route management for the dedicated backend management tunnel.

Each running builder gets exactly one host route on the backend management
tunnel (``settings.backend_route_dev``, normally ``tun1``) to its VPN server
address (``settings.backend_route_gateway``), so that backend management
traffic reaches the builder through the authorized backend identity. The
learner tunnel (``tun0``) and its ``10.0.1.0/24`` route are never modified,
and learner lab addresses are never routed through the backend tunnel.

Security invariants:
- Desired state comes only from committed database rows: every
  ``BuilderEnvironment`` with ``status == 'running'`` and a non-null private
  IP. Addresses are strictly validated with the same lab-network validation
  used by VPN authorization before any route input is produced.
- A running learner lab whose address coincides with a desired builder
  address is always excluded (a learner lab is never routed via tun1).
- All mutation goes through a single narrow root wrapper
  (``settings.backend_route_wrapper``) granted by a NOPASSWD sudoers rule
  that accepts only ``add|del <validated ipv4>``; no generic ``ip`` or shell
  is ever granted. Route reads stay unprivileged.
- Synchronization is idempotent: desired vs. current are diffed, stale
  managed routes are deleted first, then missing routes are added, under a
  module lock. Partial failures raise ``BackendRouteError`` and are never
  treated as builder failures by callers; reconciliation and startup remain
  the recovery backstop.
"""

from __future__ import annotations

import ipaddress
import logging
import subprocess
import threading
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BuilderEnvironment, LabInstance
from app.vpn_authz import (
    VpnAuthorizationError,
    _backend_identity,
    validate_lab_ip,
)

logger = logging.getLogger(__name__)

ROUTE_LOCK_TIMEOUT_SECONDS = 30.0

_ROUTE_LOCK = threading.Lock()


class BackendRouteError(Exception):
    """Failure while managing backend builder host routes."""


def _route_sync_allowed() -> bool:
    """True when touching host routes is safe at all.

    Never route-sync while Terraform dry-run or VPN are disabled: in dry-run
    the whole Gateway/Terraform surface is simulated, and with VPN disabled
    the tunnel devices may not even exist. Rollback of the managed routes
    itself is driven by ``_route_management_enabled`` inside the sync.
    """
    return bool(
        settings.vpn_enabled and not bool(settings.terraform_dry_run)
    )


def _route_management_enabled() -> bool:
    """Route management follows the backend authorization switch."""
    return bool(_route_sync_allowed() and settings.vpn_backend_authz_enabled)


def desired_builder_ips(db: Session) -> set[str]:
    """Return the canonical builder host addresses that need backend routes.

    Gated by ``settings.vpn_backend_authz_enabled`` (plus the shared VPN
    flags): when disabled the desired set is empty so a rollback also prunes
    previously managed routes. Running learner lab addresses are never
    included, even on accidental address overlap with a builder.
    """
    ips: set[str] = set()
    if not _route_management_enabled():
        return ips
    if _backend_identity(db) is None:
        return ips
    builder_rows = db.execute(
        select(BuilderEnvironment.id, BuilderEnvironment.private_ip).where(
            BuilderEnvironment.status == "running",
            BuilderEnvironment.private_ip.is_not(None),
        )
    ).all()
    for builder_id, private_ip in builder_rows:
        try:
            ips.add(validate_lab_ip(private_ip))
        except VpnAuthorizationError:
            logger.warning(
                "Skipping invalid backend host route for builder %s", builder_id
            )
    lab_ips: set[str] = set()
    lab_rows = db.execute(
        select(LabInstance.private_ip).where(
            LabInstance.status == "running",
            LabInstance.private_ip.is_not(None),
        )
    ).scalars()
    for private_ip in lab_rows:
        try:
            lab_ips.add(validate_lab_ip(private_ip))
        except VpnAuthorizationError:
            continue
    colliding = ips & lab_ips
    if colliding:
        logger.warning(
            "Skipping backend host routes overlapping running learner lab "
            "addresses: %s",
            sorted(colliding),
        )
        ips -= colliding
    return ips


def _ip_route_lines() -> list[str]:
    """Read the whole main routing table (unprivileged).

    Read without a ``dev`` filter on purpose: iproute2 omits the ``dev``
    token from device-filtered output, which would hide the interface of
    every managed route from :func:`_managed_route_ip`.
    """
    proc = subprocess.run(
        [
            "ip",
            "route",
            "show",
            "table",
            "main",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise BackendRouteError(
            f"Failed to read route state: {proc.stderr.strip()}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _managed_route_ip(line: str) -> str | None:
    """Parse exactly the /32 builder routes we manage; ignore everything else.

    iproute2 renders an IPv4 host route either with an explicit ``/32`` or,
    more commonly, as a bare address token. Both are accepted; any other
    prefix length (e.g. the learner ``/24``) is rejected.
    """
    tokens = line.split()
    if not tokens:
        return None
    address_token, slash, host_bits = tokens[0].partition("/")
    if slash and host_bits != "32":
        return None
    try:
        address = ipaddress.ip_address(address_token)
    except ValueError:
        return None
    if address.version != 4:
        return None
    if "via" not in tokens:
        return None
    gateway = tokens[tokens.index("via") + 1]
    if gateway != settings.backend_route_gateway:
        return None
    try:
        gateway_address = ipaddress.ip_address(gateway)
    except ValueError:
        return None
    if gateway_address.version != 4:
        return None
    if "dev" not in tokens:
        return None
    if tokens[tokens.index("dev") + 1] != settings.backend_route_dev:
        return None
    if "metric" not in tokens:
        return None
    try:
        metric = int(tokens[tokens.index("metric") + 1])
    except ValueError:
        return None
    if metric != int(settings.backend_route_metric):
        return None
    return str(address)


def current_managed_ips() -> set[str]:
    """Return the set of builder host addresses currently routed via tunN."""
    managed: set[str] = set()
    for line in _ip_route_lines():
        route_ip = _managed_route_ip(line)
        if route_ip is not None:
            managed.add(route_ip)
    return managed


def _run_wrapper(action: str, address: str) -> None:
    """Invoke the narrow root wrapper for one validated builder route."""
    proc = subprocess.run(
        [
            "sudo",
            "-n",
            settings.backend_route_wrapper,
            action,
            address,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise BackendRouteError(
            f"Backend route {action} failed for {address}: {proc.stderr.strip()}"
        )


def sync_backend_routes(db: Session) -> tuple[set[str], set[str]]:
    """Reconcile managed builder host routes with the desired set.

    Returns the set of added and removed addresses. Deleting stale routes
    happens first so an address that only changed path cannot linger; adds
    follow in stable order. Any wrapper failure raises ``BackendRouteError``
    (callers treat it as non-fatal and rely on reconciliation retries).
    """
    if not _route_sync_allowed():
        logger.debug("Backend route sync not allowed; skipping")
        return set(), set()
    if not _ROUTE_LOCK.acquire(timeout=ROUTE_LOCK_TIMEOUT_SECONDS):
        raise BackendRouteError("Backend route sync is busy")
    try:
        desired = desired_builder_ips(db)
        current = current_managed_ips()
        to_remove = sorted(current - desired)
        to_add = sorted(desired - current)
        for address in to_remove:
            _run_wrapper("del", address)
        for address in to_add:
            _run_wrapper("add", address)
        if to_add or to_remove:
            logger.info(
                "Backend host routes synchronized: +%s, -%s",
                len(to_add),
                len(to_remove),
            )
        return set(to_add), set(to_remove)
    finally:
        _ROUTE_LOCK.release()