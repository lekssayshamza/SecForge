"""Authenticated, derived learning read models; never award points on reads."""
from datetime import datetime
from typing import Literal
from fastapi import APIRouter,Depends,Query,Response
from pydantic import BaseModel
from sqlalchemy import select,func,case,literal,cast,String,union_all,and_
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.database import get_db
from app.models import User,Room,RoomProgress,QuestionSolve,ChallengeQuestion
from app.schemas import PublicRoomResponse
from app.lab_time import as_utc
router=APIRouter(prefix='/users/me',tags=['Learning overview'])
class Stats(BaseModel):
    points:int;rooms_completed:int;rooms_in_progress:int;questions_solved:int
class Achievement(BaseModel):
    id:str;name:str;description:str;earned:bool;earned_at:datetime|None;progress:int;target:int
class LearningRoom(BaseModel):
    id:int;name:str;category:str|None;difficulty:str|None;available:bool
    solved:int;questions:int;percentage:int|None;points:int;last_activity:datetime;completed_at:datetime|None
class Activity(BaseModel):
    id:str;kind:Literal['answer','room_completed'];room_id:int;room_name:str;available:bool;points:int;occurred_at:datetime
class ActivityPage(BaseModel):
    items:list[Activity];total:int;page:int;page_size:int
class LearningOverview(BaseModel):
    stats:Stats;achievements:list[Achievement];continue_learning:list[LearningRoom];completed_rooms:list[LearningRoom];activity:list[Activity];recommendations:list[PublicRoomResponse];recommendation_label:str

def aggregates(uid):
    return select(ChallengeQuestion.room_id.label('room_id'),func.count(QuestionSolve.id).label('solved'),func.sum(case((ChallengeQuestion.is_active,1),else_=0)).label('active_solved'),func.sum(QuestionSolve.points_awarded).label('points'),func.max(QuestionSolve.solved_at).label('last_activity')).join(QuestionSolve,QuestionSolve.question_id==ChallengeQuestion.id).where(QuestionSolve.learner_id==uid).group_by(ChallengeQuestion.room_id).subquery()

def completions(uid,agg):
    return select(RoomProgress.room_id,RoomProgress.completed_at).where(RoomProgress.learner_id==uid,RoomProgress.completed_at.is_not(None),RoomProgress.room_id.in_(select(agg.c.room_id)))

def event_query(uid,agg):
    visible=Room.status=='published'
    names=case((visible,Room.name),else_='Unavailable room').label('room_name')
    answers=select((literal('answer:')+cast(QuestionSolve.id,String)).label('id'),literal('answer').label('kind'),Room.id.label('room_id'),names,visible.label('available'),QuestionSolve.points_awarded.label('points'),QuestionSolve.solved_at.label('occurred_at')).select_from(QuestionSolve).join(ChallengeQuestion,QuestionSolve.question_id==ChallengeQuestion.id).join(Room,Room.id==ChallengeQuestion.room_id).where(QuestionSolve.learner_id==uid)
    completed=select((literal('room:')+cast(RoomProgress.id,String)).label('id'),literal('room_completed').label('kind'),Room.id.label('room_id'),names,visible.label('available'),literal(0).label('points'),RoomProgress.completed_at.label('occurred_at')).select_from(RoomProgress).join(Room,Room.id==RoomProgress.room_id).where(RoomProgress.learner_id==uid,RoomProgress.completed_at.is_not(None),RoomProgress.room_id.in_(select(agg.c.room_id)))
    return union_all(answers,completed).subquery()

def activity_page(db,uid,page,size,agg):
    events=event_query(uid,agg);total=db.scalar(select(func.count()).select_from(events))
    rows=db.execute(select(events).order_by(events.c.occurred_at.desc(),events.c.id.desc()).offset((page-1)*size).limit(size)).mappings()
    return ActivityPage(items=[Activity(**{**row,'occurred_at':as_utc(row['occurred_at'])}) for row in rows],total=total,page=page,page_size=size)

@router.get('/activity',response_model=ActivityPage)
def activity(response:Response,page:int=Query(1,ge=1,le=1000),page_size:int=Query(20,ge=1,le=50),user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    return activity_page(db,user.id,page,page_size,aggregates(user.id))

@router.get('/learning',response_model=LearningOverview)
def overview(response:Response,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';uid=user.id;agg=aggregates(uid)
    points,solved,first=db.execute(select(func.coalesce(func.sum(QuestionSolve.points_awarded),0),func.count(QuestionSolve.id),func.min(QuestionSolve.solved_at)).where(QuestionSolve.learner_id==uid)).one()
    complete=completions(uid,agg).subquery()
    completed_count=db.scalar(select(func.count()).select_from(complete))
    timestamps=list(db.scalars(select(complete.c.completed_at).order_by(complete.c.completed_at).limit(5)))
    active=select(ChallengeQuestion.room_id,func.count(ChallengeQuestion.id).label('questions')).where(ChallengeQuestion.is_active).group_by(ChallengeQuestion.room_id).subquery()
    visible=Room.status=='published'
    room_query=select(Room.id,case((visible,Room.name),else_='Unavailable room').label('name'),case((visible,Room.category),else_=None).label('category'),case((visible,Room.difficulty),else_=None).label('difficulty'),visible.label('available'),agg.c.active_solved.label('solved'),func.coalesce(active.c.questions,0).label('questions'),agg.c.points,agg.c.last_activity,RoomProgress.completed_at).join(agg,agg.c.room_id==Room.id).outerjoin(active,active.c.room_id==Room.id).outerjoin(RoomProgress,and_(RoomProgress.room_id==Room.id,RoomProgress.learner_id==uid))
    in_progress=db.scalar(select(func.count()).select_from(room_query.where(RoomProgress.completed_at.is_(None)).subquery()))
    def rooms(query):
        result=[]
        for row in db.execute(query).mappings():
            value=dict(row)
            if not value['available']:value['questions']=0
            value['percentage']=min(100,round(100*value['solved']/value['questions'])) if value['questions'] else None
            value['last_activity']=as_utc(value['last_activity']);value['completed_at']=as_utc(value['completed_at']) if value['completed_at'] else None
            result.append(LearningRoom(**value))
        return result
    earned=[]
    for identity,name,desc,count,target,when in [('first-answer','First discovery','Solve your first question.',solved,1,first),('first-room','Challenge complete','Complete your first room.',completed_count,1,timestamps[0] if timestamps else None),('five-rooms','Five investigations','Complete five rooms.',completed_count,5,timestamps[4] if len(timestamps)>=5 else None)]:
        earned.append(Achievement(id=identity,name=name,description=desc,earned=count>=target,earned_at=as_utc(when) if count>=target and when else None,progress=min(count,target),target=target))
    recommended=db.execute(select(Room.id,Room.name,Room.description,Room.category,Room.difficulty).where(visible,Room.id.not_in(select(agg.c.room_id))).order_by(case((Room.difficulty=='easy',0),(Room.difficulty=='medium',1),else_=2),Room.id.desc()).limit(4)).mappings().all()
    return LearningOverview(stats=Stats(points=points,rooms_completed=completed_count,rooms_in_progress=in_progress,questions_solved=solved),achievements=earned,continue_learning=rooms(room_query.where(visible,RoomProgress.completed_at.is_(None)).order_by(agg.c.last_activity.desc()).limit(6)),completed_rooms=rooms(room_query.where(RoomProgress.completed_at.is_not(None)).order_by(RoomProgress.completed_at.desc()).limit(4)),activity=activity_page(db,uid,1,5,agg).items,recommendations=[PublicRoomResponse(**r) for r in recommended],recommendation_label='Unstarted published rooms, with easier challenges first')
