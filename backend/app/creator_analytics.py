"""Owned, aggregate-only analytics derived from existing authoritative records."""
from datetime import datetime,timedelta
from fastapi import APIRouter,Depends,HTTPException,Query,Response
from pydantic import BaseModel
from sqlalchemy import select,func,union,exists,and_,case
from sqlalchemy.orm import Session
from app.auth import require_role
from app.database import get_db
from app.models import User,Room,LabInstance,RoomProgress,QuestionSolve,ChallengeQuestion,LearningPath,LearningPathRoom
router=APIRouter(prefix='/creator/analytics',tags=['Creator analytics'])
class RoomMetrics(BaseModel):
    id:int;title:str;room_type:str;status:str
    learners_started:int;total_starts:int;lab_requests:int;completions:int
    completion_rate:float|None;solves:int;points:int
    views:int|None=None
class PathMetrics(BaseModel):
    id:int;title:str;status:str;eligible_rooms:int;learners_started:int;learners_completed:int;completion_rate:float|None
class Trend(BaseModel):day:str;solves:int;points:int
class Analytics(BaseModel):
    owned_rooms:int;published_rooms:int;draft_rooms:int;theory_rooms:int;practical_rooms:int
    unique_learners:int;room_starts:int;total_starts:int;lab_requests:int;completions:int
    completion_rate:float|None;solves:int;points:int;published_paths:int
    views:int|None=None
    trend:list[Trend];paths:list[PathMetrics];paths_truncated:bool
class RoomPage(BaseModel):items:list[RoomMetrics];total:int;page:int;page_size:int
class QuestionMetric(BaseModel):id:int;text:str;active:bool;solve_count:int;points_awarded:int
class RoomAnalytics(BaseModel):room:RoomMetrics;questions:list[QuestionMetric]

def participation():
    # Only actual learner records; creator previews do not become engagement.
    labs=select(LabInstance.room_id.label('room_id'),LabInstance.learner_id.label('uid')).join(User,User.id==LabInstance.learner_id).join(Room,Room.id==LabInstance.room_id).where(Room.creator_id!=LabInstance.learner_id)
    quizzes=select(RoomProgress.room_id.label('room_id'),RoomProgress.learner_id.label('uid')).join(User,User.id==RoomProgress.learner_id).where(RoomProgress.attempt_count>0)
    solves=select(ChallengeQuestion.room_id.label('room_id'),QuestionSolve.learner_id.label('uid')).join(QuestionSolve,QuestionSolve.question_id==ChallengeQuestion.id).join(User,User.id==QuestionSolve.learner_id)
    return union(labs,quizzes,solves).cte('participation')
def completed_pairs():
    proof=exists(select(QuestionSolve.id).join(ChallengeQuestion,ChallengeQuestion.id==QuestionSolve.question_id).where(QuestionSolve.learner_id==RoomProgress.learner_id,ChallengeQuestion.room_id==RoomProgress.room_id))
    return select(RoomProgress.room_id.label('room_id'),RoomProgress.learner_id.label('uid')).join(User,User.id==RoomProgress.learner_id).where(RoomProgress.completed_at.is_not(None),proof).cte('completed_pairs')
def metrics_query():
    pairs=participation()
    launches=select(LabInstance.room_id,LabInstance.learner_id.label('uid'),func.count().label('n')).join(User,User.id==LabInstance.learner_id).join(Room,Room.id==LabInstance.room_id).where(Room.creator_id!=LabInstance.learner_id).group_by(LabInstance.room_id,LabInstance.learner_id).cte('launches')
    starts=select(pairs.c.room_id,func.count().label('learners_started'),func.sum(func.coalesce(launches.c.n,1)).label('total_starts'),func.sum(func.coalesce(launches.c.n,0)).label('lab_requests')).outerjoin(launches,and_(launches.c.room_id==pairs.c.room_id,launches.c.uid==pairs.c.uid)).group_by(pairs.c.room_id).cte('starts')
    done=completed_pairs();completions=select(done.c.room_id,func.count().label('completions')).group_by(done.c.room_id).cte('completions')
    awards=select(ChallengeQuestion.room_id,func.count().label('solves'),func.sum(QuestionSolve.points_awarded).label('points')).join(QuestionSolve,QuestionSolve.question_id==ChallengeQuestion.id).join(User,User.id==QuestionSolve.learner_id).group_by(ChallengeQuestion.room_id).cte('awards')
    return select(Room.id,Room.name.label('title'),Room.room_type,Room.status,*[func.coalesce(c,0).label(c.key) for c in (starts.c.learners_started,starts.c.total_starts,starts.c.lab_requests,completions.c.completions,awards.c.solves,awards.c.points)]).outerjoin(starts,starts.c.room_id==Room.id).outerjoin(completions,completions.c.room_id==Room.id).outerjoin(awards,awards.c.room_id==Room.id)
def room_metric(row):
    d=dict(row);d['completion_rate']=round(100*d['completions']/d['learners_started'],1) if d['learners_started'] else None
    return RoomMetrics(**d)
def path_metrics(db,uid):
    eligible=select(LearningPathRoom.path_id,LearningPathRoom.room_id).join(Room,Room.id==LearningPathRoom.room_id).where(Room.status=='published').cte('eligible')
    size=select(eligible.c.path_id,func.count().label('n')).group_by(eligible.c.path_id).cte('size')
    pairs=participation();done=completed_pairs()
    starters=select(eligible.c.path_id,pairs.c.uid).join(pairs,pairs.c.room_id==eligible.c.room_id).distinct().cte('path_starters')
    started=select(starters.c.path_id,func.count().label('n')).group_by(starters.c.path_id).cte('path_started')
    qualified=select(eligible.c.path_id,done.c.uid).join(done,done.c.room_id==eligible.c.room_id).join(size,size.c.path_id==eligible.c.path_id).group_by(eligible.c.path_id,done.c.uid,size.c.n).having(func.count()==size.c.n).cte('path_completed_pairs')
    completed=select(qualified.c.path_id,func.count().label('n')).group_by(qualified.c.path_id).cte('path_completed')
    query=select(LearningPath.id,LearningPath.title,LearningPath.status,func.coalesce(size.c.n,0).label('eligible_rooms'),func.coalesce(started.c.n,0).label('learners_started'),func.coalesce(completed.c.n,0).label('learners_completed')).outerjoin(size,size.c.path_id==LearningPath.id).outerjoin(started,started.c.path_id==LearningPath.id).outerjoin(completed,completed.c.path_id==LearningPath.id).where(LearningPath.creator_id==uid).order_by(LearningPath.id.desc()).limit(51)
    rows=list(db.execute(query).mappings());return [PathMetrics(**r,completion_rate=round(100*r['learners_completed']/r['learners_started'],1) if r['learners_started'] else None) for r in rows[:50]],len(rows)>50
@router.get('',response_model=Analytics)
def overview(response:Response,user:User=Depends(require_role('creator')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';data=metrics_query().where(Room.creator_id==user.id).subquery()
    totals=db.execute(select(*[func.coalesce(func.sum(data.c[n]),0).label(n) for n in ['learners_started','total_starts','lab_requests','completions','solves','points']],func.count().label('owned_rooms'),func.coalesce(func.sum(case((data.c.status=='published',1),else_=0)),0).label('published_rooms'),func.coalesce(func.sum(case((data.c.status=='draft',1),else_=0)),0).label('draft_rooms'),func.coalesce(func.sum(case((data.c.room_type=='THEORY',1),else_=0)),0).label('theory_rooms'))).mappings().one()
    pairs=participation();unique=db.scalar(select(func.count(func.distinct(pairs.c.uid))).join(Room,Room.id==pairs.c.room_id).where(Room.creator_id==user.id))
    since=(datetime.utcnow()-timedelta(days=29)).replace(hour=0,minute=0,second=0,microsecond=0)
    days=func.date(QuestionSolve.solved_at)
    trend=db.execute(select(days.label('day'),func.count().label('solves'),func.sum(QuestionSolve.points_awarded).label('points')).join(ChallengeQuestion,ChallengeQuestion.id==QuestionSolve.question_id).join(Room,Room.id==ChallengeQuestion.room_id).join(User,User.id==QuestionSolve.learner_id).where(Room.creator_id==user.id,QuestionSolve.solved_at>=since).group_by(days).order_by(days)).mappings()
    values=dict(totals);values['room_starts']=values.pop('learners_started');values.update(practical_rooms=values['owned_rooms']-values['theory_rooms'],unique_learners=unique,completion_rate=round(100*values['completions']/values['room_starts'],1) if values['room_starts'] else None)
    paths,truncated=path_metrics(db,user.id)
    return Analytics(**values,published_paths=db.scalar(select(func.count()).select_from(LearningPath).where(LearningPath.creator_id==user.id,LearningPath.status=='published')),paths=paths,paths_truncated=truncated,trend=[Trend(day=str(r['day']),solves=r['solves'],points=r['points']) for r in trend])
@router.get('/rooms',response_model=RoomPage)
def rooms(response:Response,page:int=Query(1,ge=1,le=1000),page_size:int=Query(20,ge=1,le=50),user:User=Depends(require_role('creator')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';total=db.scalar(select(func.count()).select_from(Room).where(Room.creator_id==user.id))
    rows=db.execute(metrics_query().where(Room.creator_id==user.id).order_by(Room.id.desc()).offset((page-1)*page_size).limit(page_size)).mappings()
    return RoomPage(items=[room_metric(r) for r in rows],total=total,page=page,page_size=page_size)
@router.get('/rooms/{room_id}',response_model=RoomAnalytics)
def room_detail(room_id:int,response:Response,user:User=Depends(require_role('creator')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';row=db.execute(metrics_query().where(Room.id==room_id,Room.creator_id==user.id)).mappings().first()
    if row is None:raise HTTPException(404,'Room analytics unavailable')
    counts=select(QuestionSolve.question_id,func.count().label('n'),func.sum(QuestionSolve.points_awarded).label('p')).join(User,User.id==QuestionSolve.learner_id).group_by(QuestionSolve.question_id).subquery()
    qs=db.execute(select(ChallengeQuestion.id,ChallengeQuestion.question_text.label('text'),ChallengeQuestion.is_active.label('active'),func.coalesce(counts.c.n,0).label('solve_count'),func.coalesce(counts.c.p,0).label('points_awarded')).outerjoin(counts,counts.c.question_id==ChallengeQuestion.id).where(ChallengeQuestion.room_id==room_id).order_by(ChallengeQuestion.display_order,ChallengeQuestion.id)).mappings()
    return RoomAnalytics(room=room_metric(row),questions=[QuestionMetric(**r) for r in qs])
