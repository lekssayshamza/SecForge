"""Single-use creator invitations; role changes invalidate earlier sessions."""
import hashlib
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.auth import get_current_user, account_allowed
from app.administration import audit
from app.database import get_db
from app.models import CreatorInvitation, User
from app.schemas import CreatorInvitationAccept

router = APIRouter(prefix='/creator', tags=['Creator'])

class InvitationAccepted(BaseModel):
    message: str
    reauthentication_required: bool = True

@router.post('/invitations/accept', response_model=InvitationAccepted)
def accept_creator_invitation(invitation_data: CreatorInvitationAccept,
                              current_user: User = Depends(get_current_user),
                              db: Session = Depends(get_db)):
    version = current_user.token_version
    token_hash = hashlib.sha256(invitation_data.token.encode()).hexdigest()
    invitation = db.scalar(select(CreatorInvitation).where(CreatorInvitation.token_hash == token_hash))
    if invitation is None:
        raise HTTPException(404, 'Invitation not found')
    now = datetime.utcnow()
    if invitation.expires_at <= now:
        raise HTTPException(410, 'Invitation has expired')
    if invitation.accepted_at is not None:
        raise HTTPException(409, 'Invitation has already been accepted')
    user = db.scalar(select(User).where(User.id == current_user.id).with_for_update()
                     .execution_options(populate_existing=True))
    if user is None or not account_allowed(user) or user.token_version != version or user.role != 'learner':
        raise HTTPException(403, 'Only currently authorized learners can accept creator invitations')
    # Conditional UPDATE is the single-use claim, including on SQLite. Concurrent
    # PostgreSQL requests wait and recheck this predicate after the winner commits.
    claimed = db.execute(update(CreatorInvitation).where(
        CreatorInvitation.id == invitation.id, CreatorInvitation.accepted_at.is_(None),
        CreatorInvitation.expires_at > now).values(accepted_at=now)
        .execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, 'Invitation has already been accepted')
    user.role = 'creator'
    user.token_version += 1
    audit(db, user.id, 'user', user.id, 'creator-invitation-accept', 'success',
          {'invitation_id': invitation.id, 'old_role': 'learner', 'new_role': 'creator'})
    db.commit()
    return InvitationAccepted(message='Creator access granted. Sign in again to continue.')
