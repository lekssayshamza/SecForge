import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GoogleIdentity, User
from app.schemas import LoginRequest, TokenResponse, UserResponse, PasswordChangeRequest
from app.security import create_access_token, verify_password, decode_access_token, hash_password
from app.config import settings
from app.permissions import has_creator_access, has_learning_access

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError

security = HTTPBearer()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])
_DUMMY_PASSWORD_HASH = hash_password("unusable-dummy-password-for-timing")


def account_allowed(user):
    return not user.suspended and (not (settings.production and settings.private_beta_enabled) or user.id in settings.private_beta_user_ids)


def token_response(user):
    return {"access_token": create_access_token(user_id=user.id, role=user.role, token_version=user.token_version), "token_type": "bearer"}


@router.post("/login", response_model=TokenResponse)
def login(
    login_data: LoginRequest,
    db: Session = Depends(get_db),
):
    user = db.scalar(
        select(User).where((func.lower(User.username) == login_data.username.strip().lower()) |
                           (func.lower(User.email) == login_data.username.strip().lower()))
    )

    valid = verify_password(login_data.password, user.password_hash if user and user.password_enabled else _DUMMY_PASSWORD_HASH)
    if not user or not account_allowed(user) or not user.password_enabled or not valid:
        logger.warning("Password sign-in rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password",
        )

    return token_response(user)

def get_authenticated_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (InvalidTokenError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user = db.get(User, user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if not account_allowed(user):
        raise HTTPException(status_code=403, detail="This account is not enabled for the private beta")
    if payload.get("ver", 0) != user.token_version:
        raise HTTPException(401, "Invalid or expired token")

    return user


def get_current_user(current_user: User = Depends(get_authenticated_user)):
    if current_user.must_change_password:
        raise HTTPException(403, "Change your temporary password before continuing")
    return current_user

def require_role(required_role: str):
    def role_checker(
        current_user: User = Depends(get_current_user),
    ):
        if not (has_creator_access(current_user) if required_role == "creator" else has_learning_access(current_user) if required_role == "learner" else current_user.role == required_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )

        return current_user

    return role_checker


def require_creator_or_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role not in ("creator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    return current_user

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_authenticated_user)):
    return current_user


@router.post("/password", response_model=TokenResponse)
def change_password(data: PasswordChangeRequest, user: User = Depends(get_authenticated_user), db: Session = Depends(get_db)):
    version = user.token_version
    if not user.password_enabled or not verify_password(data.current_password.get_secret_value(), user.password_hash):
        raise HTTPException(401, "Current password is incorrect")
    password = data.new_password.get_secret_value()
    if password == data.current_password.get_secret_value():
        raise HTTPException(422, "Choose a different password")
    changed = db.execute(update(User).where(User.id == user.id, User.token_version == version).values(
        password_hash=hash_password(password), must_change_password=False, token_version=version + 1
    ).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback(); raise HTTPException(409, "Your session changed; sign in again")
    db.commit(); db.refresh(user)
    return token_response(user)


@router.post("/logout", status_code=204)
def logout(user: User = Depends(get_authenticated_user), db: Session = Depends(get_db)):
    # Explicitly signs out all browser sessions for this account.
    db.execute(update(User).where(User.id == user.id).values(token_version=User.token_version + 1))
    db.commit()


@router.get("/options")
def auth_options():
    return {"google_enabled": settings.google_oauth_enabled, "registration_enabled": settings.registration_enabled,
            "password_recovery_enabled": False}


@router.get("/methods")
def auth_methods(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"password_enabled": user.password_enabled,
            "google_linked": db.scalar(select(GoogleIdentity.id).where(GoogleIdentity.user_id == user.id)) is not None}



def lock_machine_principal(db, user, role):
    """Serialize launch claims with admin access changes, preserving existing lifecycle."""
    version=user.token_version
    current=db.scalar(select(User).where(User.id==user.id).with_for_update().execution_options(populate_existing=True))
    if current is None or not account_allowed(current) or not (has_creator_access(current) if role=="creator" else has_learning_access(current) if role=="learner" else current.role==role) or current.token_version!=version:
        raise HTTPException(403,'Account authorization changed; sign in again')
    return current
