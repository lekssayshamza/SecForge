import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import require_role
from app.administration import mutation
from app.database import get_db
from app.models import CreatorInvitation, User
from app.schemas import (
    CreatorInvitationCreate,
    CreatorInvitationResponse,
)


router = APIRouter(prefix="/admin", tags=["Admin"])

@router.post(
    "/invitations",
    response_model=CreatorInvitationResponse,
    status_code=201,
)

def create_creator_invitation(
    invitation_data: CreatorInvitationCreate,
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    token = secrets.token_urlsafe(32)

    token_hash = hashlib.sha256(
        token.encode()
    ).hexdigest()

    expires_at = datetime.utcnow() + timedelta(
        days=invitation_data.expires_in_days
    )

    invitation = CreatorInvitation(
        token_hash=token_hash,
        invited_by=current_user.id,
        expires_at=expires_at,
    )

    with mutation(db,current_user,'invitation',0,'creator-invitation','access-change') as context:
        db.add(invitation);db.flush();context['invitation_id']=invitation.id
    db.refresh(invitation)

    return {
        "id": invitation.id,
        "token": token,
        "expires_at": invitation.expires_at,
    }
