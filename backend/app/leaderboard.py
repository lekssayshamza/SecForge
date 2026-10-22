"""Read-only public rankings from the immutable award ledger."""
from datetime import datetime,timedelta,timezone
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,Query,Response
from pydantic import BaseModel
from sqlalchemy import select,func,and_,or_,exists
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.permissions import LEARNING_ROLES, has_learning_access
from app.config import settings
from app.database import get_db
from app.models import User,QuestionSolve,ChallengeQuestion,RoomProgress
from app.lab_time import as_utc
Period=Literal['all-time','weekly','monthly']
router=APIRouter(tags=['Leaderboard'])
class PublicIdentity(BaseModel):
    username:str
    display_name:str|None
    avatar_style:str
    avatar_color:str
    avatar_url:str|None = None
class Ranking(PublicIdentity):
    rank:int
    points:int
    rooms_completed:int
class RankingPage(BaseModel):
    period:Period
    starts_at:datetime|None
    ends_at:datetime|None
    server_time:datetime
    items:list[Ranking]
    leaders:list[Ranking]
    total:int
    page:int
    page_size:int
class OwnRank(BaseModel):
    period:Period
    rank:int|None
    points:int
    rooms_completed:int
    eligible:bool
    reason:Literal['ranked','no-points','learners-only']
class PublicBadge(BaseModel):
    id:str
    name:str
    description:str
    earned_at:datetime
class PublicLearner(PublicIdentity):
    bio:str|None = None
    points:int
    rooms_completed:int
    questions_solved:int
    achievements:list[PublicBadge]

def utc_now():return datetime.now(timezone.utc)
def boundaries(period,now):
    now=as_utc(now)
    if period=='all-time':return None,None
    if period=='weekly':
        start=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=now.weekday())
        return start,start+timedelta(days=7)
    start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    return start, start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)

def within(column,start,end):
    # Existing scoring timestamps are naive UTC in PostgreSQL; bind naive UTC.
    return [] if start is None else [column>=start.replace(tzinfo=None),column<end.replace(tzinfo=None)]

def queries(period,now):
    start,end=boundaries(period,now)
    awards=select(QuestionSolve.learner_id.label('uid'),func.sum(QuestionSolve.points_awarded).label('points'),func.count(QuestionSolve.id).label('solved'),func.min(QuestionSolve.solved_at).label('first'),func.max(QuestionSolve.solved_at).label('last')).where(*within(QuestionSolve.solved_at,start,end)).group_by(QuestionSolve.learner_id).cte('awards')
    # A cached counter or a completion with no real solve cannot confer credit.
    proof=exists(select(QuestionSolve.id).join(ChallengeQuestion,ChallengeQuestion.id==QuestionSolve.question_id).where(QuestionSolve.learner_id==RoomProgress.learner_id,ChallengeQuestion.room_id==RoomProgress.room_id))
    completed=select(RoomProgress.learner_id.label('uid'),func.count(RoomProgress.id).label('completed')).where(RoomProgress.completed_at.is_not(None),proof,*within(RoomProgress.completed_at,start,end)).group_by(RoomProgress.learner_id).cte('completed')
    allowed=[User.role.in_(LEARNING_ROLES),User.suspended.is_(False),awards.c.points>0]
    if settings.production and settings.private_beta_enabled:allowed.append(User.id.in_(settings.private_beta_user_ids))
    rank=func.row_number().over(order_by=(awards.c.points.desc(),func.coalesce(completed.c.completed,0).desc(),awards.c.last.asc(),User.id.asc()))
    ranked=select(User.id.label('uid'),User.username,User.display_name,User.avatar_style,User.avatar_color,User.avatar_public,User.avatar_version,User.bio,User.bio_public,awards.c.points,func.coalesce(completed.c.completed,0).label('rooms_completed'),rank.label('rank')).join(awards,awards.c.uid==User.id).outerjoin(completed,completed.c.uid==User.id).where(*allowed).cte('ranked')
    return ranked,awards,completed

@router.get('/public/leaderboard',response_model=RankingPage)
def leaderboard(response:Response,period:Period='all-time',page:int=Query(1,ge=1,le=10000),page_size:int=Query(20,ge=1,le=50),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='no-store'
    now=utc_now();start,end=boundaries(period,now);ranked,_,_=queries(period,now)
    first=(page-1)*page_size+1;last=page*page_size
    total=select(func.count()).select_from(ranked).scalar_subquery()
    # One statement gives a consistent podium, page and total snapshot, no N+1.
    rows=db.execute(select(ranked,total.label('total')).where(or_(ranked.c.rank<=3,and_(ranked.c.rank>=first,ranked.c.rank<=last))).order_by(ranked.c.rank)).mappings().all()
    def item(row):return Ranking(**public_identity(row),rank=row["rank"],points=row["points"],rooms_completed=row["rooms_completed"])
    return RankingPage(period=period,starts_at=start,ends_at=end,server_time=now,items=[item(r) for r in rows if first<=r['rank']<=last],leaders=[item(r) for r in rows if r['rank']<=3],total=rows[0]['total'] if rows else 0,page=page,page_size=page_size)

@router.get('/leaderboard/me',response_model=OwnRank)
def my_rank(response:Response,period:Period='all-time',user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    ranked,awards,completed=queries(period,utc_now())
    row=db.execute(select(ranked.c.rank,func.coalesce(awards.c.points,0).label('points'),func.coalesce(completed.c.completed,0).label('completed')).select_from(User).outerjoin(awards,awards.c.uid==User.id).outerjoin(completed,completed.c.uid==User.id).outerjoin(ranked,ranked.c.uid==User.id).where(User.id==user.id)).one()
    return OwnRank(period=period,rank=row.rank,points=row.points,rooms_completed=row.completed,eligible=has_learning_access(user),reason='ranked' if row.rank else 'no-points')

@router.get('/public/learners/{username}',response_model=PublicLearner)
def public_learner(response:Response,username:str,db:Session=Depends(get_db)):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,50}',username):raise HTTPException(404,'Learner profile unavailable')
    response.headers['Cache-Control']='no-store'
    ranked,awards,_=queries('all-time',utc_now())
    # Profiles are public for active supported accounts, including unranked learners.
    _, awards, completed = queries('all-time', utc_now())
    conditions = [func.lower(User.username)==username.lower(), User.role.in_(LEARNING_ROLES), User.suspended.is_(False)]
    if settings.production and settings.private_beta_enabled:
        conditions.append(User.id.in_(settings.private_beta_user_ids))
    row=db.execute(select(User.id.label('uid'),User.username,User.display_name,User.avatar_style,User.avatar_color,User.avatar_public,User.avatar_version,User.bio,User.bio_public,func.coalesce(awards.c.points,0).label('points'),func.coalesce(awards.c.solved,0).label('solved'),awards.c.first,func.coalesce(completed.c.completed,0).label('rooms_completed')).outerjoin(awards,awards.c.uid==User.id).outerjoin(completed,completed.c.uid==User.id).where(*conditions)).mappings().first()
    if row is None:raise HTTPException(404,'Learner profile unavailable')
    proof=exists(select(QuestionSolve.id).join(ChallengeQuestion,ChallengeQuestion.id==QuestionSolve.question_id).where(QuestionSolve.learner_id==RoomProgress.learner_id,ChallengeQuestion.room_id==RoomProgress.room_id))
    times=list(db.scalars(select(RoomProgress.completed_at).where(RoomProgress.learner_id==row['uid'],RoomProgress.completed_at.is_not(None),proof).order_by(RoomProgress.completed_at).limit(5)))
    badges=[PublicBadge(id='first-answer',name='First discovery',description='Solve your first question.',earned_at=as_utc(row['first']))] if row['first'] else []
    if times:badges.append(PublicBadge(id='first-room',name='Challenge complete',description='Complete your first room.',earned_at=as_utc(times[0])))
    if len(times)==5:badges.append(PublicBadge(id='five-rooms',name='Five investigations',description='Complete five rooms.',earned_at=as_utc(times[4])))
    return PublicLearner(**public_identity(row),bio=row['bio'] if row['bio_public'] else None,points=row['points'],rooms_completed=row['rooms_completed'],questions_solved=row['solved'],achievements=badges)


def public_identity(row):
    from urllib.parse import quote
    import hashlib
    values={name:row[name] for name in ('username','display_name','avatar_style','avatar_color')}
    values['avatar_url']='/api/public/learners/'+quote(row['username'],safe='')+'/avatar?v='+hashlib.sha256(row['avatar_version'].encode()).hexdigest()[:16] if row['avatar_public'] and row['avatar_version'] else None
    return values

@router.get('/public/learners/{username}/avatar')
def public_avatar(username:str,db:Session=Depends(get_db)):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,50}',username):raise HTTPException(404,'Picture unavailable')
    credit=select(func.sum(QuestionSolve.points_awarded)).where(QuestionSolve.learner_id==User.id).correlate(User).scalar_subquery()
    conditions=[func.lower(User.username)==username.lower(),User.role.in_(LEARNING_ROLES),User.suspended.is_(False),User.avatar_public.is_(True),User.avatar_version.is_not(None)]
    if settings.production and settings.private_beta_enabled:conditions.append(User.id.in_(settings.private_beta_user_ids))
    user=db.scalar(select(User).where(*conditions))
    if user is None:raise HTTPException(404,'Picture unavailable')
    from app.avatars import get_avatar
    response=get_avatar(user)
    response.headers['Cache-Control']='no-store'
    del response.headers['Vary']
    return response
