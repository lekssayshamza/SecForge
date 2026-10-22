import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ami_manager import AMIError, AMIManager, AMITimeoutError, AMITransientError
from app.auth import get_current_user, require_role
from app.capture_identity import (
    capture_client_token,
    capture_image_name,
    capture_image_tags,
)
from app.database import get_db
from app.models import (
    BuilderEnvironment,
    CaptureRequest,
    MachineTemplate,
    Room,
    RoomTemplate,
    User,
)
from app.ops_lock import OperationInProgress, builder_lock_key, single_flight
from app.rooms import get_owned_room_or_404
from app.schemas import RoomResponse, RoomTemplateResponse
from app.vpn import sanitize_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rooms", tags=["Room Templates"])

ami_manager = AMIManager()


def _builder_has_expired(expires_at: datetime) -> bool:
    """Whether a builder's TTL has lapsed relative to UTC ``now``.

    The reconciliation worker stops expired builders, but only on its own
    interval, so a builder can read ``running`` for a short window after its
    ``expires_at``. Capture must not use such a builder.
    """
    if expires_at.tzinfo is not None:
        now = datetime.now(timezone.utc)
    else:
        now = datetime.utcnow()
    return expires_at <= now


def _primary_machine(db: Session, room_template_id: int) -> Optional[MachineTemplate]:
    return db.scalar(
        select(MachineTemplate)
        .where(MachineTemplate.room_template_id == room_template_id)
        .order_by(MachineTemplate.id)
        .limit(1)
    )


MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _normalize_idempotency_key(raw: Optional[str]) -> str:
    """Return a stable key for one logical capture request.

    A caller-supplied ``Idempotency-Key`` is used verbatim so a retry resumes
    the same capture. When absent, a fresh random key is generated so each
    request is treated as a distinct intent (never silently merged).
    """
    if raw is None:
        return uuid4().hex
    cleaned = raw.strip()
    if not cleaned:
        return uuid4().hex
    if len(cleaned) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is too long",
        )
    return cleaned


def _select_capture_builder(
    db: Session, room_id: int, creator_id: int
) -> Optional[BuilderEnvironment]:
    """Return the current live builder for a room deterministically.

    Restricted to ``running`` builders with an instance id and ordered newest
    first, so a historical stopped/failed builder row can never be selected
    over the active one.
    """
    return db.scalar(
        select(BuilderEnvironment)
        .where(
            BuilderEnvironment.room_id == room_id,
            BuilderEnvironment.creator_id == creator_id,
            BuilderEnvironment.status == "running",
            BuilderEnvironment.instance_id.is_not(None),
        )
        .order_by(
            BuilderEnvironment.created_at.desc(),
            BuilderEnvironment.id.desc(),
        )
        .limit(1)
    )


def _find_capture_request(
    db: Session, room_id: int, creator_id: int, idempotency_key: str
) -> Optional[CaptureRequest]:
    return db.scalar(
        select(CaptureRequest).where(
            CaptureRequest.room_id == room_id,
            CaptureRequest.creator_id == creator_id,
            CaptureRequest.idempotency_key == idempotency_key,
        )
    )


def _resume_capture(db: Session, capture: CaptureRequest) -> RoomTemplate:
    """Return the template already created for a replayed capture request."""
    if capture.room_template_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A capture is already in progress for this request",
        )
    template = db.get(RoomTemplate, capture.room_template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A capture is already in progress for this request",
        )
    return template


def resolve_lab_machine(
    db: Session,
    room_id: int,
    room_template_id: Optional[int] = None,
    expected_ami_id: Optional[str] = None,
) -> Optional[MachineTemplate]:
    """Resolve a saved challenge machine; database failures propagate.

    Explicit selections and published pins never substitute another version.
    Unpinned rooms use their newest available saved machine image.
    """
    if room_template_id is not None:
        template = db.get(RoomTemplate, room_template_id)
        if template is None or template.room_id != room_id:
            return None
        if expected_ami_id is None:
            machine = _primary_machine(db, template.id)
        else:
            machine = db.scalar(
                select(MachineTemplate).where(
                    MachineTemplate.room_template_id == template.id,
                    MachineTemplate.ami_id == expected_ami_id,
                    MachineTemplate.status == "available",
                ).order_by(MachineTemplate.id).limit(1)
            )
        if machine and machine.status == "available" and machine.ami_id and machine.ami_id.strip():
            return machine
        return None

    room = db.get(Room, room_id)
    if room is None:
        return None
    if room.status == "published":
        published = db.scalar(
            select(RoomTemplate).where(
                RoomTemplate.room_id == room_id,
                RoomTemplate.is_published.is_(True),
            )
        )
        if published:
            return resolve_lab_machine(db, room_id, published.id)

    return db.scalar(
        select(MachineTemplate)
        .join(RoomTemplate, RoomTemplate.id == MachineTemplate.room_template_id)
        .where(
            RoomTemplate.room_id == room_id,
            MachineTemplate.status == "available",
            MachineTemplate.ami_id.is_not(None),
            func.trim(MachineTemplate.ami_id) != "",
        )
        .order_by(RoomTemplate.version.desc(), MachineTemplate.id)
        .limit(1)
    )


def resolve_lab_ami(
    db: Session,
    room_id: int,
    room_template_id: Optional[int] = None,
    expected_ami_id: Optional[str] = None,
) -> Optional[str]:
    """Return a saved challenge image only; never a global image fallback."""
    machine = resolve_lab_machine(db, room_id, room_template_id, expected_ami_id)
    return machine.ami_id if machine else None


def _template_or_404(template_id: int, room: Room, db: Session) -> RoomTemplate:
    template = db.get(RoomTemplate, template_id)
    if not template or template.room_id != room.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room template not found",
        )
    return template


def _require_room_template_viewer(room_id: int, current_user: User, db: Session) -> Room:
    """Only owning creators may inspect machine templates."""
    if current_user.role == "learner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only creators can manage room templates",
        )
    return get_owned_room_or_404(room_id, current_user, db)


@router.post(
    "/{room_id}/templates",
    response_model=RoomTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def save_room_template(
    room_id: int,
    current_user: User = Depends(require_role("creator")),
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    room = get_owned_room_or_404(room_id, current_user, db)
    key = _normalize_idempotency_key(idempotency_key)

    # Replay of an already-started capture: return the existing version and
    # never start another EC2 image creation for the same logical intent.
    existing = _find_capture_request(db, room_id, current_user.id, key)
    if existing is not None:
        return _resume_capture(db, existing)

    builder = _select_capture_builder(db, room_id, current_user.id)
    if builder is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A running builder is required to save a room template",
        )

    if builder.expires_at is not None and _builder_has_expired(builder.expires_at):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The builder has expired; start a new builder to save a template",
        )

    try:
        with single_flight(builder_lock_key(builder.id)):
            # Re-check under the guard: a concurrent identical request may have
            # committed its capture request while the builder was being selected.
            existing = _find_capture_request(db, room_id, current_user.id, key)
            if existing is not None:
                return _resume_capture(db, existing)
            if db.scalar(select(MachineTemplate.id).where(
                MachineTemplate.source_builder_environment_id == builder.id,
                MachineTemplate.status.in_(['pending', 'creating', 'unknown']),
            ).limit(1)):
                raise HTTPException(409, 'Image capture is still being resolved. Refresh image status before another capture.')
            return _begin_capture(db, room, builder, current_user, key)
    except OperationInProgress:
        # The builder is busy. A retry of the same logical capture should resume
        # the in-progress version rather than fail, so re-check the capture
        # request (committed before the EC2 call) and never start a second one.
        existing = _find_capture_request(db, room_id, current_user.id, key)
        if existing is not None:
            return _resume_capture(db, existing)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another capture is already in progress for this builder",
        )


def _begin_capture(
    db: Session,
    room: Room,
    builder: BuilderEnvironment,
    current_user: User,
    idempotency_key: str,
) -> RoomTemplate:
    next_version = (
        db.scalar(
            select(func.max(RoomTemplate.version)).where(RoomTemplate.room_id == room.id)
        )
        or 0
    ) + 1

    capture = CaptureRequest(
        room_id=room.id,
        creator_id=current_user.id,
        idempotency_key=idempotency_key,
    )
    template = RoomTemplate(
        room_id=room.id,
        version=next_version,
        created_by=current_user.id,
    )
    db.add(capture)
    db.add(template)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _find_capture_request(db, room.id, current_user.id, idempotency_key)
        if existing is not None:
            return _resume_capture(db, existing)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A template version is already being saved; retry",
        )

    machine = MachineTemplate(
        room_template_id=template.id,
        role="primary",
        instance_type="t3.micro",
        source_builder_environment_id=builder.id,
        status="creating",
    )
    capture.room_template_id = template.id
    db.add(machine)
    from app.room_workspace import register_capture
    register_capture(room, template.id, db)
    db.commit()
    db.refresh(template)

    image_tags = capture_image_tags(
        room_id=room.id,
        template_id=template.id,
        version=next_version,
        builder_id=builder.id,
        capture_id=capture.id,
    )

    try:
        image_id = ami_manager.create_image(
            builder.instance_id,
            name=capture_image_name(room.id, next_version),
            tags=image_tags,
            client_token=capture_client_token(template.id),
        )
    except AMIError as exc:
        # Preserve a known image (successful create, failed unfreeze) and keep
        # an ambiguous outcome recoverable. Only a definitive failure is
        # terminal, and create_image is never re-issued here.
        if exc.image_id:
            machine.ami_id = exc.image_id
            machine.status = "creating"
        elif exc.ambiguous:
            machine.status = "unknown"
        else:
            machine.status = "failed"
        machine.error_message = sanitize_error(exc)
        db.commit()
        logger.warning(
            "Image capture room=%s template=%s builder=%s phase=create status=%s: %s",
            room.id, template.id, builder.id, machine.status, machine.error_message,
        )
        db.refresh(template)
        return template

    machine.ami_id = image_id
    db.commit()

    try:
        ami_manager.wait_until_available(image_id)
        machine.status = "available"
        machine.error_message = None
    except (AMITimeoutError, AMITransientError) as exc:
        # Leave the template in "creating"; reconciliation finalizes it once the
        # AMI becomes available or definitively fails. A transient polling error
        # must not orphan an image that may still exist.
        machine.error_message = sanitize_error(exc)
    except AMIError as exc:
        machine.status = "failed"
        machine.error_message = sanitize_error(exc)

    db.commit()
    if machine.error_message:
        logger.warning(
            "Image capture room=%s template=%s builder=%s phase=poll status=%s: %s",
            room.id, template.id, builder.id, machine.status, machine.error_message,
        )
    db.refresh(template)
    return template


@router.get(
    "/{room_id}/templates",
    response_model=list[RoomTemplateResponse],
)
def list_room_templates(
    room_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = _require_room_template_viewer(room_id, current_user, db)
    return db.scalars(
        select(RoomTemplate)
        .where(RoomTemplate.room_id == room.id)
        .order_by(RoomTemplate.version.desc())
    ).all()


@router.post(
    "/{room_id}/templates/{template_id}/publish",
    response_model=RoomResponse,
)
def publish_room_with_template(
    room_id: int,
    template_id: int,
    current_user: User = Depends(require_role("creator")),
    db: Session = Depends(get_db),
):
    room = get_owned_room_or_404(room_id, current_user, db)
    template = _template_or_404(template_id, room, db)
    from app.models import RoomDraft
    if db.get(RoomDraft, room.id) is not None:
        raise HTTPException(409, 'Review and publish the working copy from the creator workspace')
    from app.publication import validate_room
    validate_room(db, room, template_id)

    if room.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot publish a room in status '{room.status}'",
        )

    machine = _primary_machine(db, template.id)
    if not machine or machine.status != "available" or not machine.ami_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Template is not ready to be published",
        )

    db.execute(
        update(RoomTemplate)
        .where(RoomTemplate.room_id == room.id)
        .values(is_published=False)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    template.is_published = True
    room.status = "published"
    from app.models import RoomRelease
    from app.room_workspace import document
    from app.room_media import register_release_media
    release = RoomRelease(room_id=room.id, document=document(room, db), image_template_id=template.id, published_by=current_user.id)
    db.add(release)
    register_release_media(db, release, release.document)
    db.commit()
    db.refresh(room)
    return room
