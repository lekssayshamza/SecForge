"""Minimal public connections and private relationship controls. No activity feed."""
import re
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Request
from pydantic import BaseModel
from sqlalchemy import select, func, or_, and_, delete
from sqlalchemy.orm import Session
from app.auth import get_current_user, account_allowed
from app.database import get_db
from app.models import User, UserFollow, UserBlock
from app.permissions import LEARNING_ROLES, has_learning_access
from app.config import settings
from app.leaderboard import PublicIdentity, public_identity
from app.challenges import transaction

router = APIRouter(tags=['Social connections'])
class Connections(BaseModel):
    followers: int
    following: int
class Relationship(Connections):
    following_user: bool
    blocked: bool
    can_follow: bool
    is_self: bool
class ConnectionPage(BaseModel):
    items: list[PublicIdentity]
    total: int
    page: int
    page_size: int
class BlockPage(BaseModel):
    items: list[PublicIdentity]
    total: int
    page: int
    page_size: int

def visible():
    conditions = [User.role.in_(LEARNING_ROLES), User.suspended.is_(False)]
    if settings.production and settings.private_beta_enabled:
        conditions.append(User.id.in_(settings.private_beta_user_ids))
    return conditions

def target_user(username, db):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,50}', username):
        raise HTTPException(404, 'Profile unavailable')
    target = db.scalar(select(User).where(func.lower(User.username)==username.lower(), *visible()))
    if target is None:
        raise HTTPException(404, 'Profile unavailable')
    return target

def counts(uid, db):
    incoming = db.scalar(select(func.count()).select_from(UserFollow).join(User, User.id==UserFollow.follower_id).where(UserFollow.followed_id==uid, *visible()))
    outgoing = db.scalar(select(func.count()).select_from(UserFollow).join(User, User.id==UserFollow.followed_id).where(UserFollow.follower_id==uid, *visible()))
    return {'followers': incoming, 'following': outgoing}

def blocked_pair(a, b):
    return or_(and_(UserBlock.blocker_id==a, UserBlock.blocked_id==b), and_(UserBlock.blocker_id==b, UserBlock.blocked_id==a))

def relationship(actor, target, db):
    blocked = db.scalar(select(UserBlock.blocker_id).where(blocked_pair(actor.id, target.id)).limit(1)) is not None
    return Relationship(**counts(target.id, db), following_user=db.get(UserFollow, (actor.id,target.id)) is not None,
                        blocked=db.get(UserBlock,(actor.id,target.id)) is not None,
                        can_follow=actor.id!=target.id and not blocked, is_self=actor.id==target.id)

def lock_pair(actor, target, db):
    # All follow, block and removal writes use the same ascending account locks.
    # This makes absence checks + insertion safe across processes, including block races.
    version = actor.token_version
    users = [db.scalar(select(User).where(User.id==uid).with_for_update().execution_options(populate_existing=True)) for uid in sorted({actor.id,target.id})]
    users = [u for u in users if u is not None]
    current = next((u for u in users if u.id==actor.id),None)
    other = next((u for u in users if u.id==target.id),None)
    if current is None or not account_allowed(current) or not has_learning_access(current) or current.token_version!=version:
        raise HTTPException(403, 'Account authorization changed; sign in again')
    if other is None or not account_allowed(other) or not has_learning_access(other):
        raise HTTPException(404, 'Profile unavailable')
    if current.id==other.id:
        raise HTTPException(422, 'You cannot follow or block yourself')
    return current, other

@router.get('/public/learners/{username}/connections', response_model=Connections)
def public_counts(username: str, response: Response, db: Session=Depends(get_db)):
    response.headers['Cache-Control']='no-store'
    return counts(target_user(username,db).id,db)

@router.get('/public/learners/{username}/connections/{direction}', response_model=ConnectionPage)
def connections(username: str, direction: Literal['followers','following'], response: Response,
                page: int=Query(1,ge=1,le=1000), page_size: int=Query(20,ge=1,le=50), db: Session=Depends(get_db)):
    response.headers['Cache-Control']='no-store'
    target=target_user(username,db)
    incoming=direction=='followers'
    other=UserFollow.follower_id if incoming else UserFollow.followed_id
    mine=UserFollow.followed_id if incoming else UserFollow.follower_id
    query=select(User, UserFollow.created_at).join(UserFollow,other==User.id).where(mine==target.id,*visible())
    total=db.scalar(select(func.count()).select_from(query.subquery()))
    rows=db.execute(query.order_by(UserFollow.created_at.desc(),User.id).offset((page-1)*page_size).limit(page_size)).all()
    return ConnectionPage(items=[PublicIdentity(**public_identity(u.__dict__)) for u,_ in rows],total=total,page=page,page_size=page_size)

@router.get('/social/users/{username}', response_model=Relationship)
def state(username: str, response: Response, user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    return relationship(user,target_user(username,db),db)

@router.put('/social/users/{username}/follow', response_model=Relationship)
@router.delete('/social/users/{username}/follow', response_model=Relationship)
def follow(username: str, response: Response, request: Request,
           user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    with transaction(db):
        actor,target=lock_pair(user,target_user(username,db),db)
        existing=db.get(UserFollow,(actor.id,target.id))
        if request.method=='PUT':
            if db.scalar(select(UserBlock.blocker_id).where(blocked_pair(actor.id,target.id)).limit(1)) is not None:
                raise HTTPException(403,'Following is unavailable for this account')
            if existing is None:
                db.add(UserFollow(follower_id=actor.id,followed_id=target.id))
        elif existing is not None:
            db.delete(existing)
        db.flush()
        result=relationship(actor,target,db)
    return result

@router.put('/social/users/{username}/block', response_model=Relationship)
@router.delete('/social/users/{username}/block', response_model=Relationship)
def block(username: str, response: Response, request: Request,
          user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    with transaction(db):
        actor,target=lock_pair(user,target_user(username,db),db)
        existing=db.get(UserBlock,(actor.id,target.id))
        if request.method=='PUT':
            if existing is None: db.add(UserBlock(blocker_id=actor.id,blocked_id=target.id))
            db.execute(delete(UserFollow).where(or_(and_(UserFollow.follower_id==actor.id,UserFollow.followed_id==target.id),and_(UserFollow.follower_id==target.id,UserFollow.followed_id==actor.id))))
        elif existing is not None: db.delete(existing)
        db.flush();result=relationship(actor,target,db)
    return result
