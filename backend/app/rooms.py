from app.permissions import has_creator_access
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import (Room, RoomTemplate, User, RoomProgress, RoomRelease,
                        BuilderEnvironment, LabInstance, ChallengeQuestion, QuestionSolve)
from app.schemas import RoomCreate, RoomResponse, RoomUpdate

router = APIRouter(prefix="/rooms", tags=["Rooms"])

VALID_DIFFICULTIES = {"easy", "medium", "hard"}
VALID_STATUSES = {"draft", "pending_review", "published", "rejected"}


def validate_difficulty(difficulty: str):
    if difficulty not in VALID_DIFFICULTIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid difficulty. Must be one of: {', '.join(VALID_DIFFICULTIES)}",
        )


def validate_category(category: str):
    if not category or len(category.strip()) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category cannot be empty",
        )
    if len(category) > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category must be at most 50 characters",
        )


def validate_name(name: str):
    if not name or len(name.strip()) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Name cannot be empty",
        )
    if len(name) > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Name must be at most 100 characters",
        )


def validate_description(description: str | None):
    if description is not None and len(description) > 2000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Description must be at most 2000 characters",
        )


@router.post(
    "",
    response_model=RoomResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_room(
    room_data: RoomCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not has_creator_access(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only creators can create rooms",
        )

    validate_difficulty(room_data.difficulty)
    validate_category(room_data.category)
    validate_name(room_data.name)
    validate_description(room_data.description)

    room = Room(
        creator_id=current_user.id,
        name=room_data.name,
        description=room_data.description,
        category=room_data.category,
        difficulty=room_data.difficulty,
        room_type=room_data.room_type, objectives=room_data.objectives, prerequisites=room_data.prerequisites,
        status="draft",
        exposed_ports=[r.model_dump() for r in room_data.exposed_ports],
    )

    if room_data.categories is not None and room_data.categories[0] != room_data.category:
        raise HTTPException(422, 'The primary category must be the first selected category')
    from app.categories import set_room_categories
    set_room_categories(room, room_data.categories or [room_data.category])
    db.add(room)
    db.commit()
    db.refresh(room)

    return room


@router.get(
    "",
    response_model=List[RoomResponse],
)
def list_rooms(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role == "learner":
        rooms = db.scalars(
            select(Room).where(Room.status == "published")
        ).all()
    elif has_creator_access(current_user):
        rooms = db.scalars(
            select(Room).where(Room.creator_id == current_user.id)
        ).all()
    elif current_user.role == "admin":
        rooms = db.scalars(select(Room).where((Room.status == "published") | (Room.creator_id == current_user.id))).all()
    else:
        rooms = []

    return rooms


def get_room_or_404(room_id: int, current_user: User, db: Session) -> Room:
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    if current_user.role == "learner":
        if room.status != "published":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found",
            )
    elif has_creator_access(current_user):
        if room.creator_id != current_user.id and room.status != "published":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found",
            )
    elif current_user.role != "admin" or room.status != "published" and room.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Room not found",
        )

    return room


@router.get(
    "/{room_id}",
    response_model=RoomResponse,
)
def get_room(
    room_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = get_room_or_404(room_id, current_user, db)
    return room


@router.patch(
    "/{room_id}",
    response_model=RoomResponse,
)
def update_room(
    room_id: int,
    room_data: RoomUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    if not has_creator_access(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only creators can update rooms",
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

    if room.status == "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot edit a published room",
        )

    from app.room_workspace import legacy_write_guard
    legacy_write_guard(room, db)

    if room_data.name is not None:
        validate_name(room_data.name)
        room.name = room_data.name

    if room_data.description is not None:
        validate_description(room_data.description)
        room.description = room_data.description

    if room_data.category is not None:
        validate_category(room_data.category)
        room.category = room_data.category
    if room_data.categories is not None and room_data.category is not None and room_data.categories[0] != room_data.category:
        raise HTTPException(422, 'The primary category must be the first selected category')
    if room_data.categories is not None or room_data.category is not None:
        from app.categories import set_room_categories
        set_room_categories(room, room_data.categories or [room_data.category])

    if room_data.difficulty is not None:
        validate_difficulty(room_data.difficulty)
        room.difficulty = room_data.difficulty

    if 'exposed_ports' in room_data.model_fields_set:
        room.exposed_ports = [r.model_dump() for r in room_data.exposed_ports]
    for field in ("objectives", "prerequisites"):
        if getattr(room_data, field) is not None:setattr(room, field, getattr(room_data, field))
    room.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(room)

    if background_tasks is not None and 'exposed_ports' in room_data.model_fields_set:
        from app.network_sync import sync_room_background
        background_tasks.add_task(sync_room_background, room_id)
    return room


def get_owned_room_or_404(room_id: int, current_user: User, db: Session) -> Room:
    if not has_creator_access(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only creators can manage room publication",
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

    return room


@router.post(
    "/{room_id}/publish",
    response_model=RoomResponse,
)
def publish_room(
    room_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = get_owned_room_or_404(room_id, current_user, db)

    from app.room_workspace import legacy_write_guard
    legacy_write_guard(room, db)

    if room.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot publish a room in status '{room.status}'",
        )

    if room.room_type != "THEORY":
        raise HTTPException(409, "Publish a ready saved template version for practical rooms")
    from app.publication import validate_room
    validate_room(db, room)
    room.status = "published"
    room.updated_at = datetime.utcnow()
    from app.models import RoomRelease
    from app.room_workspace import document
    from app.room_media import register_release_media
    release = RoomRelease(room_id=room.id, document=document(room, db), published_by=current_user.id)
    db.add(release)
    register_release_media(db, release, release.document)

    db.commit()
    db.refresh(room)

    return room


@router.post(
    "/{room_id}/unpublish",
    response_model=RoomResponse,
)
def unpublish_room(
    room_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = get_owned_room_or_404(room_id, current_user, db)

    if room.status != "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot unpublish a room in status '{room.status}'",
        )

    room.status = "draft"
    room.updated_at = datetime.utcnow()

    # Unpinning means a re-publish must explicitly select a version again.
    db.execute(
        update(RoomTemplate)
        .where(RoomTemplate.room_id == room.id)
        .values(is_published=False)
        .execution_options(synchronize_session=False)
    )

    db.commit()
    db.refresh(room)

    return room


@router.delete(
    "/{room_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_room(
    room_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not has_creator_access(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only creators can delete rooms",
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

    if room.status in ("published", "pending_review"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete a room with status '{room.status}'",
        )

    # Progress is independent of lab cleanup and must not be erased by room deletion.
    if db.scalar(select(RoomProgress.id).where(RoomProgress.room_id == room.id).limit(1)) is not None or db.scalar(select(QuestionSolve.id).join(ChallengeQuestion).where(ChallengeQuestion.room_id == room.id).limit(1)) is not None:
        raise HTTPException(409, "Rooms with learner progress cannot be deleted.")

    # Ordinary content deletion must not delete image history, erase machine
    # ownership/state, or make billable resources invisible to reconciliation.
    # Keep these references even after Stop; infrastructure retirement is a
    # separate explicit operator procedure, never an AMI-delete side effect.
    if any(db.scalar(select(model.id).where(model.room_id == room.id).limit(1)) is not None
           for model in (RoomTemplate, RoomRelease, BuilderEnvironment, LabInstance)):
        raise HTTPException(409, 'This room has retained publication or machine history. Unpublish it instead; its images and resource records are preserved.')

    db.delete(room)
    db.commit()

    return None
