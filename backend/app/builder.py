from datetime import datetime, timedelta
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role, require_creator_or_admin, lock_machine_principal
from app.backend_routes import BackendRouteError, sync_backend_routes
from app.cleanup import cleanup_failed
from app.config import settings
from app.database import get_db
from app.lab_manager import LabManager, TerraformError
from app.models import BuilderEnvironment, Room, User
from app.ops_lock import OperationInProgress
from app.schemas import BuilderResponse, BuilderStartRequest
from app.vpn_authz import (
    VpnAuthorizationError,
    authorize_builder_access,
    sync_vpn_authz,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rooms", tags=["Builder"])

MAX_TTL_HOURS = 4
MIN_TTL_HOURS = 1

ACTIVE_STATUSES = ("pending", "provisioning", "running", "stopping")
EXTENDABLE_STATUSES = ("pending", "provisioning", "running")
STOPPABLE_STATUSES = ("pending", "provisioning", "running")

lab_manager = LabManager()


def _current_builder_query(room_id: int):
    # Historical rows must not hide an active environment or make Stop appear
    # complete while a newer builder is still billable.
    return (
        select(BuilderEnvironment)
        .where(BuilderEnvironment.room_id == room_id)
        .order_by(
            case((BuilderEnvironment.status.in_(ACTIVE_STATUSES), 0), else_=1),
            BuilderEnvironment.id.desc(),
        )
        .limit(1)
    )


def get_builder_or_404(
    room_id: int, current_user: User, db: Session
) -> BuilderEnvironment:
    builder = db.scalar(
        _current_builder_query(room_id)
    )
    if not builder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )

    if builder.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )
    elif current_user.role not in ("creator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )

    return builder


@router.post(
    "/{room_id}/builder",
    response_model=BuilderResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_builder(
    room_id: int,
    request: BuilderStartRequest,
    current_user: User = Depends(require_role("creator")),
    db: Session = Depends(get_db),
):
    lock_machine_principal(db,current_user,"creator")
    if request.ttl_hours < MIN_TTL_HOURS or request.ttl_hours > MAX_TTL_HOURS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"TTL must be between {MIN_TTL_HOURS} and {MAX_TTL_HOURS} hours",
        )

    room = db.scalar(select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True))
    if not room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    if room.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    if room.status not in ("draft", "published"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Builder cannot be started in this room state",
        )

    expires_at = datetime.utcnow() + timedelta(hours=request.ttl_hours)

    source_ami_id = None
    ssh_auth_mode, ssh_guest_username = 'key', None
    if room.status == 'published':
        from app.templates import resolve_lab_machine
        from app.models import RoomTemplate
        pinned = db.scalar(select(RoomTemplate.id).where(RoomTemplate.room_id == room.id, RoomTemplate.is_published.is_(True)))
        machine = resolve_lab_machine(db, room.id, pinned) if pinned else None
        if machine is None:
            raise HTTPException(409, 'The published image is unavailable. Existing content is unchanged.')
        source_ami_id = machine.ami_id
        source_builder = db.get(BuilderEnvironment, machine.source_builder_environment_id) if machine.source_builder_environment_id else None
        if source_builder:
            ssh_auth_mode, ssh_guest_username = source_builder.ssh_auth_mode, source_builder.ssh_guest_username

    builder = BuilderEnvironment(
        room_id=room.id,
        creator_id=current_user.id,
        status="pending",
        terraform_state_path="",  # placeholder, will be set after flush
        expires_at=expires_at,
        source_ami_id=source_ami_id,
        ssh_auth_mode=ssh_auth_mode,
        ssh_guest_username=ssh_guest_username,
    )

    db.add(builder)
    try:
        db.flush()  # assign builder.id
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Creator already has an active builder environment",
        )

    builder.terraform_state_path = str(
        Path(settings.builder_terraform_workspace_root) / f"builder-{builder.id}"
    )
    db.commit()
    db.refresh(builder)

    # Trigger provisioning asynchronously (for now, synchronously in test context)
    # In production, this would be a background task
    try:
        lab_manager.start_builder(builder)
        if builder.status == "running":
            try:
                authorize_builder_access(db, builder)
            except VpnAuthorizationError:
                # Fail closed: a running builder must never exist without its
                # creator VPN authorization being applied.
                logger.warning(
                    "VPN authorization update failed for builder %s", builder.id
                )
                builder.status = "failed"
                builder.error_message = "VPN authorization update failed"
            else:
                # Host route creation for the dedicated backend tunnel is
                # never a builder failure: a missing route only affects
                # backend->builder reachability and is retried by
                # reconciliation and startup syncs.
                try:
                    sync_backend_routes(db)
                except BackendRouteError as exc:
                    logger.warning(
                        "Backend host route sync failed for builder %s: %s",
                        builder.id,
                        exc,
                    )
        db.commit()
        db.refresh(builder)
    except TerraformError:
        # Error already set on builder_env by LabManager
        db.commit()
        db.refresh(builder)
        # Don't raise - return the builder with failed status
        # The API will return the builder with status=failed
    except OperationInProgress:
        db.refresh(builder)

    # Terraform already provisions the configured EC2 key pair. Personal-key
    # registration is retired; never add an extra guest SSH trust source here.
    return builder


@router.get(
    "/{room_id}/builder",
    response_model=BuilderResponse,
)
def get_builder(
    room_id: int,
    current_user: User = Depends(require_creator_or_admin),
    db: Session = Depends(get_db),
):
    builder = get_builder_or_404(room_id, current_user, db)
    return builder


@router.post(
    "/{room_id}/builder/stop",
    response_model=BuilderResponse,
)
def stop_builder(
    room_id: int,
    current_user: User = Depends(require_role("creator")),
    db: Session = Depends(get_db),
):
    builder = db.scalar(
        _current_builder_query(room_id).with_for_update()
    )
    if not builder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )

    if builder.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )
    elif current_user.role not in ("creator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )

    if builder.status in ("stopping", "stopped"):
        return builder
    if builder.status not in STOPPABLE_STATUSES and not cleanup_failed(builder):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot stop builder in status '{builder.status}'",
        )

    claimed = db.execute(
        update(BuilderEnvironment)
        .where(BuilderEnvironment.id == builder.id, BuilderEnvironment.status == builder.status)
        .values(status="stopping", updated_at=datetime.utcnow())
    )
    db.commit()
    db.refresh(builder)
    if not claimed.rowcount:
        if builder.status in ("stopping", "stopped"):
            return builder
        raise HTTPException(status_code=409, detail="Builder state changed; refresh before stopping")

    # Remove the creator's builder authorization promptly; the periodic
    # reconciliation and startup syncs remain the fail-safe backstops.
    try:
        sync_vpn_authz(db)
    except VpnAuthorizationError as exc:
        logger.warning(
            "VPN authorization removal failed for builder %s: %s", builder.id, exc
        )

    # Drop the backend tunnel's host route for this builder promptly.
    try:
        sync_backend_routes(db)
    except BackendRouteError as exc:
        logger.warning(
            "Backend host route removal failed for builder %s: %s", builder.id, exc
        )

    try:
        lab_manager.stop_builder(builder)
        db.commit()
        db.refresh(builder)
    except TerraformError:
        # Error already set on builder_env by LabManager
        db.commit()
        db.refresh(builder)
    except OperationInProgress:
        db.refresh(builder)

    return builder


@router.post(
    "/{room_id}/builder/extend",
    response_model=BuilderResponse,
)
def extend_builder(
    room_id: int,
    request: BuilderStartRequest,
    current_user: User = Depends(require_role("creator")),
    db: Session = Depends(get_db),
):
    lock_machine_principal(db,current_user,"creator")
    if request.ttl_hours < MIN_TTL_HOURS or request.ttl_hours > MAX_TTL_HOURS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"TTL must be between {MIN_TTL_HOURS} and {MAX_TTL_HOURS} hours",
        )

    builder = db.scalar(
        select(BuilderEnvironment)
        .where(BuilderEnvironment.room_id == room_id)
        .with_for_update()
    )
    if not builder:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )

    if builder.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )
    elif current_user.role not in ("creator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Builder environment not found for this room",
        )

    if builder.status not in EXTENDABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot extend builder in status '{builder.status}'",
        )

    new_expires_at = builder.expires_at + timedelta(hours=request.ttl_hours)
    if new_expires_at > builder.created_at + timedelta(hours=MAX_TTL_HOURS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Extension would exceed maximum TTL of {MAX_TTL_HOURS} hours",
        )

    builder.expires_at = new_expires_at
    builder.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(builder)

    return builder
