import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_creator_or_admin, require_role, lock_machine_principal
from app.database import SessionLocal, get_db
from app.cleanup import cleanup_failed
from app.lab_manager import LabManager, TemplateResolutionError, TerraformError
from app.lab_time import INITIAL_TIME, EXTENSION_TIME, EXTENSION_WINDOW, MAX_TIME, MAX_EXTENSIONS, as_utc, utc_now
from app.models import LabInstance, MachineTemplate, Room, RoomTemplate, User
from app.ops_lock import OperationInProgress
from app.schemas import LabResponse, LabStartRequest, LabExtendRequest
from app.templates import resolve_lab_machine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rooms", tags=["Labs"])
labs_me_router = APIRouter(prefix="/labs", tags=["Labs"])

MIN_TTL_HOURS = 1
MAX_TTL_HOURS = 4

ACTIVE_STATUSES = ("pending", "provisioning", "running", "stopping")
STOPPABLE_STATUSES = ("pending", "provisioning", "running")


def provision_lab_background(
    lab_id: int,
    room_template_id: int | None = None,
    expected_ami_id: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        lab = db.get(LabInstance, lab_id)
        if lab is None:
            return
        try:
            manager = LabManager()
            if expected_ami_id is not None:
                manager.start_lab_instance(lab, db, room_template_id, expected_ami_id)
            elif room_template_id is not None:
                manager.start_lab_instance(lab, db, room_template_id)
            else:
                manager.start_lab_instance(lab, db)
        except TerraformError as e:
            logger.warning(f"Lab {lab_id} provisioning failed: {e.message}")
        except TemplateResolutionError as e:
            logger.warning(f"Lab {lab_id} template resolution failed: {e}")
        except OperationInProgress:
            logger.warning(f"Lab {lab_id} provisioning already in progress")
        except Exception:
            logger.exception(f"Unexpected error provisioning lab {lab_id}")
    finally:
        db.close()


def destroy_lab_background(lab_id: int) -> None:
    db = SessionLocal()
    try:
        lab = db.get(LabInstance, lab_id)
        if lab is None or lab.status != "stopping":
            return
        try:
            LabManager().stop_lab_instance(lab, db)
        except TerraformError as e:
            logger.warning("Failed to destroy lab %s: %s", lab_id, e.message)
        except OperationInProgress:
            logger.info("Destroy already in progress for lab %s, deferring", lab_id)
        except Exception:
            logger.exception("Unexpected error destroying lab %s", lab_id)
    finally:
        db.close()


def validate_ttl(ttl_hours: int):
    if ttl_hours < MIN_TTL_HOURS or ttl_hours > MAX_TTL_HOURS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"TTL must be between {MIN_TTL_HOURS} and {MAX_TTL_HOURS} hours",
        )


def get_published_room_or_404(room_id: int, db: Session) -> Room:
    room = db.scalar(select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True))
    if not room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )
    if room.status != "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Room is not published",
        )
    return room


def lab_query(
    room_id: int,
    learner_id: int,
    active_only: bool,
    for_update: bool,
):
    query = select(LabInstance).where(
        LabInstance.room_id == room_id,
        LabInstance.learner_id == learner_id,
    )
    if active_only:
        query = query.where(LabInstance.status.in_(ACTIVE_STATUSES))
    query = query.order_by(LabInstance.created_at.desc(), LabInstance.id.desc())
    if for_update:
        query = query.with_for_update()
    return query


def get_learner_lab_or_404(
    room_id: int, learner_id: int, db: Session, for_update: bool = False
) -> LabInstance:
    lab = db.scalar(lab_query(room_id, learner_id, active_only=True, for_update=for_update))
    if lab:
        return lab

    lab = db.scalar(lab_query(room_id, learner_id, active_only=False, for_update=for_update))
    if not lab:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lab instance not found for this room",
        )

    return lab


def _learning_room_access(room_id, user, db):
    room = db.get(Room, room_id)
    if room is None or room.creator_id == user.id:
        raise HTTPException(404, "Learning lab not found")


def _primary_available_machine(
    db: Session, room_template_id: int
) -> MachineTemplate | None:
    return db.scalar(
        select(MachineTemplate)
        .where(
            MachineTemplate.room_template_id == room_template_id,
            MachineTemplate.status == "available",
            MachineTemplate.ami_id.is_not(None),
            func.trim(MachineTemplate.ami_id) != "",
        )
        .order_by(MachineTemplate.id)
        .limit(1)
    )


def _test_room_or_404(room_id: int, current_user: User, db: Session) -> Room:
    room = db.get(Room, room_id)
    if room is None or (
        room.creator_id != current_user.id
    ):
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@contextmanager
def _test_lab_database_errors(db: Session):
    """Expose a retryable service error without database/connection details."""
    try:
        yield
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503, detail="Test lab service unavailable; refresh status and retry",
        ) from None


@router.post(
    "/{room_id}/lab",
    response_model=LabResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_lab(
    room_id: int,
    request: LabStartRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_role("learner")),
    db: Session = Depends(get_db),
):

    lock_machine_principal(db,current_user,"learner")
    validate_ttl(request.ttl_hours)
    room = get_published_room_or_404(room_id, db)
    if room.creator_id == current_user.id:
        raise HTTPException(403, "Use the creator test machine for your own room")

    existing = db.scalar(lab_query(room_id, current_user.id, active_only=True, for_update=False))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Learner already has an active lab instance for this room",
        )

    try:
        machine = resolve_lab_machine(db, room_id)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to resolve the room challenge image; retry later",
        ) from None
    if machine is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Room has no available saved challenge image",
        )
    template_id, image_id = machine.room_template_id, machine.ami_id

    now = utc_now()
    lab = LabInstance(
        room_id=room_id,
        learner_id=current_user.id,
        status="pending",
        session_started_at=now,
        expires_at=now + INITIAL_TIME,
        extensions_used=0,
    )

    db.add(lab)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Learner already has an active lab instance for this room",
        )

    db.commit()
    db.refresh(lab)

    background_tasks.add_task(provision_lab_background, lab.id, template_id, image_id)

    return lab


@router.post(
    "/{room_id}/templates/{template_id}/lab",
    response_model=LabResponse,
    status_code=status.HTTP_201_CREATED,
)
def test_launch_lab(
    room_id: int,
    template_id: int,
    request: LabStartRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_creator_or_admin),
    db: Session = Depends(get_db),
):

    lock_machine_principal(db,current_user,"creator")
    """Authorized test launch from an explicitly selected saved template version.

    Restricted to the room owner or an admin. The AMI is always derived
    server-side from the persisted template; a resolution failure at
    provisioning time fails the lab instead of falling back to the global AMI.
    Learner launches independently select the room's saved challenge image.
    """
    with _test_lab_database_errors(db):
        _test_room_or_404(room_id, current_user, db)
        template = db.get(RoomTemplate, template_id)
        if template is None or template.room_id != room_id:
            raise HTTPException(status_code=404, detail="Template not found for this room")

        machine = _primary_available_machine(db, template.id)
        if machine is None:
            raise HTTPException(status_code=409, detail="Selected template version has no available AMI")
        # Freeze the selection for background revalidation, just as learner launch does.
        selected_template_id, image_id = template.id, machine.ami_id
        validate_ttl(request.ttl_hours)
        if db.scalar(lab_query(room_id, current_user.id, active_only=True, for_update=False)):
            raise HTTPException(status_code=409, detail="An active lab instance already exists for this room")

        lab = LabInstance(
            room_id=room_id, learner_id=current_user.id, status="pending",
            expires_at=utc_now() + timedelta(hours=request.ttl_hours),
        )
        db.add(lab)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="An active lab instance already exists for this room")
        db.refresh(lab)
        background_tasks.add_task(provision_lab_background, lab.id, selected_template_id, image_id)
        return lab


@router.get("/{room_id}/test-lab", response_model=LabResponse)
def get_my_test_lab(
    room_id: int,
    current_user: User = Depends(require_creator_or_admin),
    db: Session = Depends(get_db),
):
    with _test_lab_database_errors(db):
        _test_room_or_404(room_id, current_user, db)
        # Test launch stores its caller in learner_id; never select another user's lab.
        return get_learner_lab_or_404(room_id, current_user.id, db)


@router.post("/{room_id}/test-lab/stop", response_model=LabResponse)
def stop_test_lab(
    room_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_creator_or_admin),
    db: Session = Depends(get_db),
):
    with _test_lab_database_errors(db):
        _test_room_or_404(room_id, current_user, db)
        return _stop_owned_lab(room_id, background_tasks, current_user, db)


@router.get(
    "/{room_id}/lab",
    response_model=LabResponse,
)
def get_my_lab(
    room_id: int,
    current_user: User = Depends(require_role("learner")),
    db: Session = Depends(get_db),
):
    _learning_room_access(room_id, current_user, db)
    return get_learner_lab_or_404(room_id, current_user.id, db)


@router.post("/{room_id}/lab/extend", response_model=LabResponse)
def extend_lab(
    room_id: int,
    request: LabExtendRequest,
    current_user: User = Depends(require_role("learner")),
    db: Session = Depends(get_db),
):
    _learning_room_access(room_id, current_user, db)
    """The session ID and expected quota act as an idempotent extension claim."""
    try:
        lab = db.scalar(select(LabInstance).where(
            LabInstance.id == request.lab_id, LabInstance.room_id == room_id,
            LabInstance.learner_id == current_user.id,
        ).with_for_update().execution_options(populate_existing=True))
        if lab is None:
            raise HTTPException(404, "Lab session not found")
        now = utc_now()  # Read time AFTER waiting for a concurrent transaction.
        if lab.status != "running" or lab.session_started_at is None or as_utc(lab.expires_at) <= now or lab.expired_at is not None:
            raise HTTPException(409, "Only an unexpired running learner lab can be extended")
        expected = request.expected_extensions_used
        if lab.extensions_used > expected:
            db.commit()  # Replay: return the committed result, without awarding more time.
            db.refresh(lab)
            return lab
        if lab.extensions_used != expected or expected >= MAX_EXTENSIONS:
            raise HTTPException(409, "No extension is available for this session")
        if as_utc(lab.expires_at) - now > EXTENSION_WINDOW:
            raise HTTPException(409, "Extend when 30 minutes or less remain")
        deadline = as_utc(lab.expires_at) + EXTENSION_TIME
        if deadline > as_utc(lab.session_started_at) + MAX_TIME:
            raise HTTPException(409, "The three-hour session limit has been reached")
        claimed = db.execute(update(LabInstance).where(
            LabInstance.id == lab.id, LabInstance.status == "running",
            LabInstance.extensions_used == expected, LabInstance.expires_at == lab.expires_at,
            LabInstance.expires_at > now, LabInstance.expired_at.is_(None),
        ).values(expires_at=deadline, extensions_used=expected + 1, updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
        db.commit()
        db.refresh(lab)
        if not claimed.rowcount:
            raise HTTPException(409, "Lab changed; refresh its status before extending")
        return lab
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Lab time service is temporarily unavailable") from None
    except HTTPException:
        db.rollback()
        raise


@router.post(
    "/{room_id}/lab/stop",
    response_model=LabResponse,
)
def stop_lab(
    room_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_role("learner")),
    db: Session = Depends(get_db),
):
    _learning_room_access(room_id, current_user, db)
    return _stop_owned_lab(room_id, background_tasks, current_user, db)


def _stop_owned_lab(room_id: int, background_tasks: BackgroundTasks, current_user: User, db: Session):
    """Shared row-locked, conditional Stop claim for both authenticated flows."""
    lab = get_learner_lab_or_404(
        room_id, current_user.id, db, for_update=True
    )

    if lab.status in ("stopping", "stopped"):
        return lab
    if lab.status not in STOPPABLE_STATUSES and not cleanup_failed(lab):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot stop lab instance in status '{lab.status}'",
        )

    claimed = db.execute(
        update(LabInstance)
        .where(LabInstance.id == lab.id, LabInstance.status == lab.status)
        .values(status="stopping", updated_at=datetime.utcnow())
    )
    db.commit()
    db.refresh(lab)
    if not claimed.rowcount:
        if lab.status in ("stopping", "stopped"):
            return lab
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot stop lab instance in status '{lab.status}'",
        )

    background_tasks.add_task(destroy_lab_background, lab.id)

    return lab


@labs_me_router.get(
    "/me",
    response_model=List[LabResponse],
)
def list_my_labs(
    current_user: User = Depends(require_role("learner")),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(LabInstance)
        .join(Room, Room.id == LabInstance.room_id)
        .where(LabInstance.learner_id == current_user.id, Room.creator_id != current_user.id)
        .order_by(LabInstance.created_at.desc(), LabInstance.id.desc())
    ).all()
