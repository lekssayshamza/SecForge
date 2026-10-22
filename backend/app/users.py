from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.schemas import ProfileUpdate, UserCreate, UserResponse
from app.security import hash_password
from app.auth import get_current_user
from app.config import settings


router = APIRouter(prefix="/users", tags=["Users"])


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user(user_data: UserCreate, db: Session = Depends(get_db)):
    if not settings.registration_enabled:
        raise HTTPException(403, "Registration is closed for the private beta")
    existing_user = db.scalar(
        select(User).where(
            (func.lower(User.username) == user_data.username)
            | (func.lower(User.email) == user_data.email)
        )
    )

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already exists",
        )

    user = User(
        username=user_data.username,
        email=user_data.email,
        password_hash=hash_password(user_data.password),
        role="learner",
    )

    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Username or email already exists") from None
    db.refresh(user)

    return user


@router.patch("/me", response_model=UserResponse)
def update_profile(
    data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # The principal is selected by JWT user ID, never by a request target or
    # username. Only explicitly supplied public profile fields are writable.
    updates = data.model_dump(exclude_unset=True)
    try:
        if "username" in updates and db.scalar(select(User.id).where(
            func.lower(User.username) == updates["username"], User.id != current_user.id,
        )) is not None:
            raise HTTPException(409, "That username is already in use.")
        for field, value in updates.items():
            setattr(current_user, field, value)
        db.commit()
        db.refresh(current_user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That username is already in use.") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Profile service is temporarily unavailable. Please retry.") from None
    return current_user
