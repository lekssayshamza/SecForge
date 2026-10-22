"""Dynamic VPN authorization for the shared gateway.

Desired authorization state is derived entirely from committed database
state: every running lab with a private IP joined with its learner's active
VPN identity, and every running builder with a private IP joined with its
creator's active VPN identity, yield one ``(VPN IP, private IP)`` pair each.
Identities are always per-user (never per-builder or per-lab). The
desired state is applied atomically to the gateway in two parts:

- the named ``lab_pairs`` set lives in the dedicated
  ``table inet secforge-vpn`` (destroyed and recreated each sync) as the
  canonical projection of the desired state, and
- a single marked accept rule lives inside the pre-existing
  ``inet filter forward`` base chain, directly above its policy DROP. An
  ``accept`` in a separate base chain is not final in nftables — the packet
  would continue into ``inet filter forward`` and be dropped — so the
  authorization verdict must be rendered into the same chain as the policy.
  nftables cannot reference a named set across tables (verified on the
  gateway's nft 1.0.9: ``@table.set`` references fail with "No such file or
  directory"), so the marked rule embeds the same validated elements as an
  inline anonymous concat set, rendered from the same desired state as
  ``lab_pairs`` within the same transaction so the two can never diverge.
  The rule is replaced idempotently: its handle is queried from the live
  ruleset, the old rule is deleted, and the new one is added, all within the
  same transaction. With no pairs no rule is rendered at all, so
  authorization remains fail-closed.

Security invariants:
- Lab and learner addresses are strictly validated before any nft input is
  rendered; only canonical ``ipaddress`` output ever reaches the transaction,
  so no caller-supplied metacharacter can appear in nft syntax.
- The gateway is only ever asked to run ``sudo -n nft -a list chain inet
  filter forward`` (read-only) and ``sudo -n nft -f -`` with the rendered
  transaction on stdin through the existing argv-validated SSH wrapper; no
  shell is used.
- Normally the transaction only ever destroys/recreates the dedicated
  ``secforge-vpn`` table and deletes/re-adds the one uniquely marked
  authorization rule; established/related handling, input rules, the
  forward policy DROP, and every other rule are never modified. The explicitly
  enabled builder internet policy additionally manages its own marked rule
  and table; it never NATs VPN traffic or authorizes learner internet access.
- Authorization is applied before a lab or a builder is committed
  ``running``; if the update fails the entity is marked ``failed`` and is
  never silently reported as running without VPN access.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.permissions import has_creator_access
from app.config import settings
from app.models import BuilderEnvironment, LabInstance, User, VpnIdentity
from app.lab_time import utc_now, deadline_passed
from app.vpn import (
    VpnManager,
    VpnManagerError,
    sanitize_error,
    validate_vpn_ip,
)

logger = logging.getLogger(__name__)

NFT_TABLE_NAME = "secforge-vpn"
NFT_SET_NAME = "lab_pairs"
FILTER_CHAIN = "inet filter forward"
AUTHZ_RULE_COMMENT = "secforge-vpn-authz"
AUTHZ_RULE_MARKER = f'comment "{AUTHZ_RULE_COMMENT}"'
_TUNNEL_INTERFACE = "tun0"
BACKEND_COMMON_NAME = "sf-backend"
BACKEND_SYSTEM_ROLE = "system"
_HANDLE_RE = re.compile(r"# handle (\d+)\s*$")
SYNC_LOCK_TIMEOUT_SECONDS = 30.0

_SYNC_LOCK = threading.Lock()


class VpnAuthorizationError(Exception):
    """Failure while applying dynamic learner-to-lab VPN authorization."""


def _lab_network() -> "ipaddress.IPv4Network":
    try:
        network = ipaddress.ip_network(settings.lab_private_cidr, strict=True)
    except ValueError:
        raise VpnAuthorizationError(
            "Configured lab private network is invalid"
        ) from None
    if network.version != 4:
        raise VpnAuthorizationError("Configured lab private network is invalid")
    return network


def validate_lab_ip(lab_ip: str) -> str:
    """Validate a lab private address inside the configured lab network."""
    if not isinstance(lab_ip, str):
        raise VpnAuthorizationError("Lab address is invalid")
    try:
        address = ipaddress.ip_address(lab_ip)
    except ValueError:
        raise VpnAuthorizationError("Lab address is invalid") from None
    if address.version != 4:
        raise VpnAuthorizationError("Lab address is invalid")
    network = _lab_network()
    if address in (network.network_address, network.broadcast_address):
        raise VpnAuthorizationError("Lab address is reserved")
    if address not in network:
        raise VpnAuthorizationError("Lab address is outside the lab network")
    return str(address)


def _backend_identity(db: Session) -> VpnIdentity | None:
    """Return the active dedicated backend management identity, if any.

    Identified by its system common name and role (never a hardcoded user
    id) so the exact database user id stays an implementation detail.
    """
    return db.scalar(
        select(VpnIdentity)
        .join(User, VpnIdentity.user_id == User.id)
        .where(
            VpnIdentity.common_name == BACKEND_COMMON_NAME,
            VpnIdentity.revoked_at.is_(None),
            User.role == BACKEND_SYSTEM_ROLE,
        )
    )


def _backend_builder_pairs(db: Session) -> set[tuple[str, str]]:
    """Return ``(backend VPN IP, running builder private IP)`` pairs.

    The dedicated backend identity is authorized to reach every currently
    running builder the backend needs to manage. It is gated by
    ``settings.vpn_backend_authz_enabled`` (off by default) so enabling it
    is an explicit deployment step. Fail-closed: no running builders, an
    absent/revoked backend identity, or an invalid backend address yield no
    pairs; invalid builder addresses are skipped individually. Learner and
    creator pairs are never affected.
    """
    pairs: set[tuple[str, str]] = set()
    if not bool(settings.vpn_backend_authz_enabled):
        return pairs
    identity = _backend_identity(db)
    if identity is None:
        return pairs
    try:
        backend_ip = validate_vpn_ip(identity.vpn_ip)
    except VpnManagerError:
        logger.warning(
            "Skipping backend builder authorization: invalid backend VPN address"
        )
        return pairs
    builder_ips = db.execute(
        select(BuilderEnvironment.private_ip).where(
            BuilderEnvironment.status == "running",
            BuilderEnvironment.private_ip.is_not(None),
        )
    ).scalars()
    for private_ip in builder_ips:
        try:
            pairs.add((backend_ip, validate_lab_ip(private_ip)))
        except VpnAuthorizationError:
            logger.warning(
                "Skipping invalid backend authorization row for builder %s",
                private_ip,
            )
    return pairs


def desired_pairs(db: Session) -> set[tuple[str, str]]:
    """Return the committed ``(VPN IP, private IP)`` authorized pairs.

    Covers three ownership-scoped flows from the same committed database
    state:

    - learner VPN IP → private IP of each learner's running lab,
    - creator VPN IP → private IP of each creator's running builder, and
    - (when ``vpn_backend_authz_enabled`` is on) the dedicated backend
      system identity's VPN IP → private IP of every running builder.

    The learner and creator joins require a non-revoked per-user VPN
    identity; the identity is always the authenticated user's own
    (per-user, never per-builder). The backend identity is matched by its
    system common name and is authorized only to running builders.
    """
    learner_rows = db.execute(
        select(LabInstance.learner_id, LabInstance.private_ip, VpnIdentity.vpn_ip)
        .join(VpnIdentity, VpnIdentity.user_id == LabInstance.learner_id)
        .where(
            LabInstance.status == "running",
            LabInstance.expires_at > utc_now(),
            LabInstance.private_ip.is_not(None),
            VpnIdentity.revoked_at.is_(None),
        )
    ).all()
    builder_rows = db.execute(
        select(
            BuilderEnvironment.creator_id,
            BuilderEnvironment.private_ip,
            VpnIdentity.vpn_ip,
        )
        .join(VpnIdentity, VpnIdentity.user_id == BuilderEnvironment.creator_id)
        .where(
            BuilderEnvironment.status == "running",
            BuilderEnvironment.private_ip.is_not(None),
            VpnIdentity.revoked_at.is_(None),
        )
    ).all()
    pairs: set[tuple[str, str]] = set()
    for owner_id, private_ip, vpn_ip in learner_rows:
        try:
            pairs.add((validate_vpn_ip(vpn_ip), validate_lab_ip(private_ip)))
        except (VpnManagerError, VpnAuthorizationError):
            logger.warning(
                "Skipping invalid VPN authorization row for learner %s", owner_id
            )
    for owner_id, private_ip, vpn_ip in builder_rows:
        try:
            pairs.add((validate_vpn_ip(vpn_ip), validate_lab_ip(private_ip)))
        except (VpnManagerError, VpnAuthorizationError):
            logger.warning(
                "Skipping invalid VPN authorization row for builder owner %s",
                owner_id,
            )
    pairs.update(_backend_builder_pairs(db))
    return pairs


def find_filter_rule_handle(listing: str, comment: str = AUTHZ_RULE_COMMENT) -> int | None:
    """Extract the handle of the marked authorization rule from an nft listing.

    Returns ``None`` when the marked rule is absent. Raises
    ``VpnAuthorizationError`` when the listing is unusable or the marked rule
    appears more than once, so a broken query can never silently produce
    duplicate rules.
    """
    if not isinstance(listing, str):
        raise VpnAuthorizationError("Gateway rule listing is invalid")
    handles: list[int] = []
    for line in listing.splitlines():
        if f'comment "{comment}"' not in line:
            continue
        match = _HANDLE_RE.search(line)
        if match is None:
            raise VpnAuthorizationError("Gateway rule listing is invalid")
        handle = int(match.group(1))
        if handle <= 0:
            raise VpnAuthorizationError("Gateway rule listing is invalid")
        handles.append(handle)
    if len(handles) > 1:
        raise VpnAuthorizationError("Duplicate VPN authorization rules detected")
    return handles[0] if handles else None


def render_nft_transaction(
    pairs: Iterable[tuple[str, str]],
    existing_rule_handle: int | None = None,
    *,
    builder_ips: Iterable[str] | None = None,
    existing_egress_handle: int | None = None,
) -> str:
    """Render the atomic nft transaction for the desired authorization state.

    Order matters for in-transaction validation: the existing marked filter
    rule (which embeds the authorized pairs) is deleted first, then the
    dedicated table is destroyed and recreated with the desired set contents,
    and finally the marked accept rule carrying the same elements inline is
    re-added inside ``inet filter forward`` so it terminates the forward hook
    before the policy DROP. With no pairs the rule is omitted entirely,
    leaving only the empty dedicated set (fail-closed).
    """
    if existing_rule_handle is not None:
        if (
            not isinstance(existing_rule_handle, int)
            or isinstance(existing_rule_handle, bool)
            or existing_rule_handle <= 0
        ):
            raise VpnAuthorizationError("Authorization rule handle is invalid")
    validated: set[tuple[str, str]] = set()
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise VpnAuthorizationError("Authorization pair is invalid")
        learner_ip, lab_ip = pair
        try:
            validated.add((validate_vpn_ip(learner_ip), validate_lab_ip(lab_ip)))
        except VpnManagerError as exc:
            raise VpnAuthorizationError(
                f"Learner address is invalid: {sanitize_error(exc)}"
            ) from None
    ordered = sorted(
        validated,
        key=lambda pair: (
            int(ipaddress.ip_address(pair[0])),
            int(ipaddress.ip_address(pair[1])),
        ),
    )
    lines: list[str] = []
    if existing_rule_handle is not None:
        lines.append(
            f"delete rule {FILTER_CHAIN} handle {existing_rule_handle}"
        )
    lines.extend(
        [
            f"destroy table inet {NFT_TABLE_NAME}",
            f"table inet {NFT_TABLE_NAME} {{",
            f"\tset {NFT_SET_NAME} {{",
            "\t\ttype ipv4_addr . ipv4_addr",
        ]
    )
    if ordered:
        lines.append("\t\telements = {")
        for index, (learner_ip, lab_ip) in enumerate(ordered):
            comma = "," if index < len(ordered) - 1 else ""
            lines.append(f"\t\t\t{learner_ip} . {lab_ip}{comma}")
        lines.append("\t\t}")
    lines.append("\t}")
    lines.append("}")
    lines.append("")
    if ordered:
        elements = ", ".join(
            f"{learner_ip} . {lab_ip}" for learner_ip, lab_ip in ordered
        )
        lines.append(
            f"add rule {FILTER_CHAIN} iifname \"{_TUNNEL_INTERFACE}\" "
            f"ip saddr . ip daddr {{ {elements} }} "
            f"accept {AUTHZ_RULE_MARKER}"
        )
        lines.append("")
    if existing_egress_handle is not None:
        if type(existing_egress_handle) is not int or existing_egress_handle <= 0:
            raise VpnAuthorizationError("Builder internet rule handle is invalid")
        if existing_egress_handle == existing_rule_handle:
            raise VpnAuthorizationError("Authorization rule handles conflict")
        lines.append(f"delete rule {FILTER_CHAIN} handle {existing_egress_handle}")
    if builder_ips is not None:
        from app.builder_egress import render_policy
        policy, accept = render_policy(builder_ips)
        lines.append(policy)
        if accept:
            lines.append(accept)
    elif existing_egress_handle is not None:
        from app.builder_egress import TABLE
        lines.append(f"destroy table ip {TABLE}")
    return "\n".join(lines)


def _apply_pairs(
    db: Session, pairs: set[tuple[str, str]], *, builder_override: BuilderEnvironment | None = None
) -> set[tuple[str, str]]:
    try:
        manager = VpnManager()
        listing = manager.list_forward_chain_rules()
        handle = find_filter_rule_handle(listing)
        if settings.builder_internet_enabled:
            from app.builder_egress import COMMENT, desired_builder_ips
            from app.lab_time import as_utc
            ips = desired_builder_ips(db)
            if (builder_override is not None and builder_override.status == "running"
                    and builder_override.instance_id and builder_override.private_ip
                    and as_utc(builder_override.expires_at) > utc_now()):
                owner = db.get(User, builder_override.creator_id)
                if owner is not None and has_creator_access(owner):
                    ips.add(validate_lab_ip(builder_override.private_ip))
            egress_handle = find_filter_rule_handle(listing, COMMENT)
            manager.sync_forward_rules(pairs, existing_rule_handle=handle,
                                      builder_ips=ips, existing_egress_handle=egress_handle)
        else:
            from app.builder_egress import COMMENT
            egress_handle = find_filter_rule_handle(listing, COMMENT)
            if egress_handle is not None:
                # Turning the flag off must revoke a previously installed
                # policy, including established sessions. Keep the empty
                # guard until the operator removes the shared route.
                manager.sync_forward_rules(
                    pairs, existing_rule_handle=handle, builder_ips=set(),
                    existing_egress_handle=egress_handle,
                )
            else:
                manager.sync_forward_rules(pairs, existing_rule_handle=handle)
    except VpnManagerError as exc:
        raise VpnAuthorizationError(
            f"VPN authorization update failed: {sanitize_error(exc)}"
        ) from None
    logger.info("VPN authorization synchronized with %d pair(s)", len(pairs))
    return pairs


def _sync_enabled() -> bool:
    return bool(settings.vpn_enabled and settings.vpn_authz_enabled)


def sync_vpn_authz(db: Session) -> set[tuple[str, str]] | None:
    """Apply the full desired authorization state to the gateway."""
    if not _sync_enabled():
        return None
    if settings.terraform_dry_run:
        logger.debug("Dry-run mode: skipping VPN authorization sync")
        return None
    if not _SYNC_LOCK.acquire(timeout=SYNC_LOCK_TIMEOUT_SECONDS):
        raise VpnAuthorizationError("VPN authorization sync is busy")
    try:
        return _apply_pairs(db, desired_pairs(db))
    finally:
        _SYNC_LOCK.release()


def authorize_lab_access(
    db: Session, lab: LabInstance
) -> set[tuple[str, str]] | None:
    """Apply desired authorization including a lab before it is committed running."""
    if not _sync_enabled():
        return None
    if settings.terraform_dry_run:
        logger.debug("Dry-run mode: skipping lab VPN authorization")
        return None
    if not _SYNC_LOCK.acquire(timeout=SYNC_LOCK_TIMEOUT_SECONDS):
        raise VpnAuthorizationError("VPN authorization sync is busy")
    try:
        pairs = set(desired_pairs(db))
        if lab.private_ip is not None and lab.status in ("pending", "provisioning", "running") and not deadline_passed(lab):
            lab_ip = validate_lab_ip(lab.private_ip)
            vpn_ip_row = db.scalar(
                select(VpnIdentity.vpn_ip).where(
                    VpnIdentity.user_id == lab.learner_id,
                    VpnIdentity.revoked_at.is_(None),
                )
            )
            if vpn_ip_row is None:
                logger.info(
                    "No VPN identity for learner %s; lab access deferred",
                    lab.learner_id,
                )
            else:
                try:
                    learner_ip = validate_vpn_ip(vpn_ip_row)
                except VpnManagerError:
                    logger.warning(
                        "Invalid VPN identity address for learner %s",
                        lab.learner_id,
                    )
                else:
                    pairs.add((learner_ip, lab_ip))
        return _apply_pairs(db, pairs)
    finally:
        _SYNC_LOCK.release()


def authorize_builder_access(
    db: Session, builder: BuilderEnvironment
) -> set[tuple[str, str]] | None:
    """Apply desired authorization including a builder before it is committed running.

    Mirrors :func:`authorize_lab_access`: a just-started builder has its
    ``running`` status, EC2 instance id, and private IP set in memory but not
    yet flushed or committed, and sessions use ``autoflush=False``, so the
    committed-state query in :func:`desired_pairs` cannot see it. The builder's
    own pair(s) are therefore added explicitly from the in-memory row so a
    newly running builder is never silently omitted from the gateway. Fail
    closed: a builder that is not ``running`` or has no private IP contributes
    no pair, an invalid builder address raises, and a missing or invalid
    creator/backend identity simply yields no pair for that identity.
    """
    if not _sync_enabled():
        return None
    if settings.terraform_dry_run:
        logger.debug("Skipping builder VPN authorization")
        return None
    if not _SYNC_LOCK.acquire(timeout=SYNC_LOCK_TIMEOUT_SECONDS):
        raise VpnAuthorizationError("VPN authorization sync is busy")
    try:
        pairs = set(desired_pairs(db))
        if builder.status == "running" and builder.private_ip is not None:
            builder_ip = validate_lab_ip(builder.private_ip)
            creator_vpn_ip = db.scalar(
                select(VpnIdentity.vpn_ip).where(
                    VpnIdentity.user_id == builder.creator_id,
                    VpnIdentity.revoked_at.is_(None),
                )
            )
            if creator_vpn_ip is None:
                logger.info(
                    "No VPN identity for creator %s; builder access deferred",
                    builder.creator_id,
                )
            else:
                try:
                    pairs.add((validate_vpn_ip(creator_vpn_ip), builder_ip))
                except VpnManagerError:
                    logger.warning(
                        "Invalid VPN identity address for creator %s",
                        builder.creator_id,
                    )
            if bool(settings.vpn_backend_authz_enabled):
                identity = _backend_identity(db)
                if identity is not None:
                    try:
                        pairs.add((validate_vpn_ip(identity.vpn_ip), builder_ip))
                    except VpnManagerError:
                        logger.warning(
                            "Skipping invalid backend authorization for builder %s",
                            builder.id,
                        )
        return _apply_pairs(db, pairs, builder_override=builder)
    finally:
        _SYNC_LOCK.release()
