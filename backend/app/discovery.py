"""Published metadata and private learner read models; no scoring or cloud writes."""
from typing import Literal
from fastapi import APIRouter,Depends,Query,Response
from pydantic import BaseModel
from sqlalchemy import select,func,and_
from sqlalchemy.orm import Session
from app.auth import require_role
from app.database import get_db
from app.models import Room,User,LearningPath,LearningPathRoom,RoomProgress,ChallengeQuestion,QuestionSolve,RoomBookmark,PathBookmark
from app.schemas import PublicRoomResponse
from app.learning_paths import PathSummary,PathPage
from app.public_rooms import FIELDS
from app.discovery_query import Sort,ProgressFilter,filters,ordering
from app.categories import SLUGS
router=APIRouter(prefix='/discovery',tags=['Learner discovery'])
class RoomCard(PublicRoomResponse):
    points_total:int=0
    bookmarked:bool=False
    progress:Literal['not-started','in-progress','completed']='not-started'
class RoomPage(BaseModel):
    items:list[RoomCard];total:int;page:int;page_size:int;categories:list[str]
class PathCard(PathSummary):
    bookmarked:bool=False
    rooms_completed:int=0
    percent:int=0
class LearnerPathPage(BaseModel):
    items:list[PathCard];total:int;page:int;page_size:int;categories:list[str]

def room_proof(uid):
    return select(ChallengeQuestion.room_id.label('room_id'),func.count().label('solves')).join(QuestionSolve,QuestionSolve.question_id==ChallengeQuestion.id).where(QuestionSolve.learner_id==uid).group_by(ChallengeQuestion.room_id).subquery()

def room_page(db,q='',category='',difficulty='',room_type='',sort='newest',progress='all',page=1,page_size=24,uid=None,saved=False):
    conditions=filters(q,category,difficulty,Room,Room.name)
    if room_type:conditions.append(Room.room_type==room_type)
    points=select(ChallengeQuestion.room_id,func.sum(ChallengeQuestion.points).label('points')).where(ChallengeQuestion.is_active).group_by(ChallengeQuestion.room_id).subquery()
    query=select(*FIELDS,func.coalesce(points.c.points,0).label('points_total')).outerjoin(points,points.c.room_id==Room.id)
    if uid is not None:
        proof=room_proof(uid)
        query=query.outerjoin(RoomBookmark,and_(RoomBookmark.room_id==Room.id,RoomBookmark.user_id==uid)).outerjoin(RoomProgress,and_(RoomProgress.room_id==Room.id,RoomProgress.learner_id==uid)).outerjoin(proof,proof.c.room_id==Room.id).add_columns(RoomBookmark.user_id.label('bookmark_user'),RoomProgress.completed_at,proof.c.solves)
        if progress=='completed':conditions.extend([proof.c.solves.is_not(None),RoomProgress.completed_at.is_not(None)])
        elif progress=='in-progress':conditions.extend([proof.c.solves.is_not(None),RoomProgress.completed_at.is_(None)])
        elif progress=='not-started':conditions.append(proof.c.solves.is_(None))
        if saved:conditions.append(RoomBookmark.user_id.is_not(None))
    query=query.where(*conditions)
    total=db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows=db.execute(query.order_by(*ordering(Room,Room.name,sort)).offset((page-1)*page_size).limit(page_size)).mappings()
    from app.categories import attach_categories
    items=[]
    for row in attach_categories(db, rows):
        data={k:row[k] for k in [c.key for c in FIELDS]};data['points_total']=row['points_total'];data['categories']=row['categories']
        if uid is not None:data.update(bookmarked=row['bookmark_user'] is not None,progress='completed' if row['solves'] and row['completed_at'] else 'in-progress' if row['solves'] else 'not-started')
        items.append(RoomCard(**data))
    return RoomPage(items=items,total=total,page=page,page_size=page_size,categories=list(SLUGS))

def path_page(db,q='',category='',difficulty='',sort='newest',page=1,page_size=24,uid=None,saved=False):
    conditions=filters(q,category,difficulty,LearningPath,LearningPath.title)
    members=select(LearningPathRoom.path_id,func.count().label('n')).join(Room,Room.id==LearningPathRoom.room_id).where(Room.status=='published').group_by(LearningPathRoom.path_id).subquery()
    query=select(LearningPath,func.coalesce(members.c.n,0).label('room_count')).outerjoin(members,members.c.path_id==LearningPath.id)
    if uid is not None:
        proof=room_proof(uid)
        completed=select(LearningPathRoom.path_id,func.count().label('n')).join(Room,Room.id==LearningPathRoom.room_id).join(RoomProgress,and_(RoomProgress.room_id==Room.id,RoomProgress.learner_id==uid)).join(proof,proof.c.room_id==Room.id).where(Room.status=='published',RoomProgress.completed_at.is_not(None)).group_by(LearningPathRoom.path_id).subquery()
        query=query.outerjoin(PathBookmark,and_(PathBookmark.path_id==LearningPath.id,PathBookmark.user_id==uid)).outerjoin(completed,completed.c.path_id==LearningPath.id).add_columns(PathBookmark.user_id.label('bookmark_user'),func.coalesce(completed.c.n,0).label('completed'))
        if saved:conditions.append(PathBookmark.user_id.is_not(None))
    query=query.where(*conditions);total=db.scalar(select(func.count()).select_from(query.order_by(None).subquery()));items=[]
    for row in db.execute(query.order_by(*ordering(LearningPath,LearningPath.title,sort)).offset((page-1)*page_size).limit(page_size)):
        p,n=row[0],row[1];data={k:getattr(p,k) for k in ['id','title','description','difficulty','category','objectives','status']};data['room_count']=n
        if uid is not None:data.update(bookmarked=row[2] is not None,rooms_completed=row[3],percent=round(100*row[3]/n) if n else 0)
        items.append(PathCard(**data))
    return LearnerPathPage(items=items,total=total,page=page,page_size=page_size,categories=list(SLUGS))

@router.get('/rooms',response_model=RoomPage)
def rooms(response:Response,q:str=Query('',max_length=100),category:str=Query('',max_length=400),difficulty:Literal['','easy','medium','hard']='',room_type:Literal['','THEORY','PRACTICAL']='',sort:Sort='newest',progress:ProgressFilter='all',page:int=Query(1,ge=1,le=1000),page_size:int=Query(24,ge=1,le=48),user:User=Depends(require_role('learner')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';return room_page(db,q,category,difficulty,room_type,sort,progress,page,page_size,user.id)
@router.get('/paths',response_model=LearnerPathPage)
def paths(response:Response,q:str=Query('',max_length=100),category:str=Query('',max_length=50),difficulty:Literal['','easy','medium','hard']='',sort:Sort='newest',page:int=Query(1,ge=1,le=1000),page_size:int=Query(24,ge=1,le=48),user:User=Depends(require_role('learner')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';return path_page(db,q,category,difficulty,sort,page,page_size,user.id)
