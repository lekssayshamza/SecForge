"""Per-user VPN profile issuance endpoints.

Flow A (gateway-first): reuse an existing active identity if present,
otherwise ensure the client certificate exists on the gateway, write the
CCD, then persist the identity. Database unique constraints on
``user_id``, ``common_name`` and ``vpn_ip`` plus an allocation retry loop
make concurrent duplicate allocation impossible.

Available to any authenticated user (learner or creator); the issued
identity and profile are always the caller's own, derived from the
authenticated principal — never from client-supplied parameters. Creators
use the same per-user identity/profile to reach their active builder
environment; learners use it to reach their active lab.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import User, VpnIdentity
from app.profile_identity import vpn_profile_filename
from app.vpn import (
    VpnManager,
    VpnManagerError,
    build_client_profile,
    common_name_for_user,
    iter_candidate_ips,
)
from app.vpn_authz import VpnAuthorizationError, sync_vpn_authz

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vpn", tags=["VPN"])

PROFILE_MEDIA_TYPE = "application/x-openvpn-profile"

_SERVICE_UNAVAILABLE = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="VPN service is temporarily unavailable",
)
_GATEWAY_FAILURE = HTTPException(
    status_code=status.HTTP_502_BAD_GATEWAY,
    detail="VPN gateway operation failed",
)
_IDENTITY_REVOKED = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="VPN access for this account has been disabled",
)


class VpnIdentityRevoked(Exception):
    """Raised when the learner's stored VPN identity is revoked."""


class VpnAddressPoolExhausted(Exception):
    """Raised when no learner VPN addresses remain."""


def _find_identity(db: Session, user_id: int) -> VpnIdentity | None:
    return db.scalar(select(VpnIdentity).where(VpnIdentity.user_id == user_id))


def _require_active(identity: VpnIdentity | None) -> VpnIdentity | None:
    if identity is not None and identity.revoked_at is not None:
        raise VpnIdentityRevoked
    return identity


def _ensure_certificate(manager: VpnManager, common_name: str) -> None:
    try:
        manager.issue_client_certificate(common_name)
    except VpnManagerError as issue_error:
        try:
            manager.read_client_certificate(common_name)
        except VpnManagerError:
            raise issue_error
        logger.info("Reusing existing VPN certificate for %s", common_name)


def _candidate_ips(db: Session) -> list[str]:
    used = list(db.scalars(select(VpnIdentity.vpn_ip)).all()) + settings.vpn_reserved_client_ips
    return list(iter_candidate_ips(used))


def _create_identity(
    db: Session, user: User, manager: VpnManager
) -> VpnIdentity:
    common_name = common_name_for_user(user.id)
    _ensure_certificate(manager, common_name)
    for vpn_ip in _candidate_ips(db):
        manager.write_client_ccd(common_name, vpn_ip)
        try:
            identity = VpnIdentity(
                user_id=user.id,
                common_name=common_name,
                vpn_ip=vpn_ip,
            )
            db.add(identity)
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = _find_identity(db, user.id)
            if existing is not None:
                _require_active(existing)
                manager.write_client_ccd(existing.common_name, existing.vpn_ip)
                return existing
            continue
        return identity
    raise VpnAddressPoolExhausted


@router.get("/profile")
def download_vpn_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    if not settings.vpn_enabled:
        raise _SERVICE_UNAVAILABLE

    manager = VpnManager()
    try:
        identity = _find_identity(db, current_user.id)
        _require_active(identity)
        if identity is None:
            identity = _create_identity(db, current_user, manager)
        try:
            sync_vpn_authz(db)
        except VpnAuthorizationError as exc:
            logger.warning(
                "VPN authorization sync failed after profile issuance: %s", exc
            )
        profile = build_client_profile(
            manager, identity.common_name, identity.vpn_ip
        )
    except VpnIdentityRevoked:
        raise _IDENTITY_REVOKED from None
    except VpnAddressPoolExhausted:
        raise _SERVICE_UNAVAILABLE from None
    except VpnManagerError as exc:
        logger.warning("VPN profile request failed: %s", exc)
        raise _GATEWAY_FAILURE from None

    return Response(
        content=profile,
        media_type=PROFILE_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{vpn_profile_filename(current_user.username)}"',
            "Cache-Control": "no-store",
        },
    )
