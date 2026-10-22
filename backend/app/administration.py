"""Explicit, authenticated administration. No delete or lab/AWS mutation routes."""
from contextlib import contextmanager
from datetime import datetime,timezone
from typing import Literal,Generic,TypeVar
import json
from fastapi import APIRouter,Depends,HTTPException,Query,Response
from pydantic import BaseModel,Field,model_validator
from sqlalchemy import select,func,exists,update,case
from sqlalchemy.orm import Session
from app.auth import require_role
from app.database import get_db
from app.models import User,Room,LearningPath,LabInstance,BuilderEnvironment,AdminAudit,AdminControl,RoomProgress,QuestionSolve,ChallengeQuestion,RoomTemplate
from app.publication import validate_room,validate_path
from app.lab_time import as_utc
router=APIRouter(prefix='/admin',tags=['Administration'],dependencies=[Depends(require_role('admin'))])
Role=Literal['learner','creator','admin']
Reason=Literal['account-review','policy-violation','access-change','content-review','owner-request']
T=TypeVar('T')
class Page(BaseModel,Generic[T]):items:list[T];total:int;page:int;page_size:int
class Account(BaseModel):
    id:int;username:str;display_name:str|None;role:str;suspended:bool;created_at:datetime;points:int;completed_rooms:int
class AccountChange(BaseModel):
    role:Role|None=None;suspended:bool|None=None
    expected_role:Role;expected_suspended:bool;confirm_username:str=Field(min_length=3,max_length=50);reason:Reason
    model_config={'extra':'forbid'}
    @model_validator(mode='after')
    def one(self):
        if (self.role is None)==(self.suspended is None):raise ValueError('Request one explicit change at a time')
        return self
class ActionResult(BaseModel):changed:bool
class Content(BaseModel):
    id:int;title:str;creator_id:int;creator:str;room_type:str|None=None;category:str;difficulty:str;status:str;moderation_hold:bool;created_at:datetime;updated_at:datetime
class Moderation(BaseModel):
    publish:bool;template_id:int|None=Field(default=None,ge=1);confirmation:str=Field(max_length=60);reason:Reason
    model_config={'extra':'forbid'}
class LabRecord(BaseModel):
    id:int;kind:Literal['learner','builder'];owner_id:int;owner:str;room_id:int;room:str;status:str
    started_at:datetime;expires_at:datetime;remaining_seconds:int;cleanup_status:str;issue:str|None
class LabPage(Page[LabRecord]):server_time:datetime;source:str='Application lifecycle records; use Infrastructure for live EC2 inventory.'
class AuditRecord(BaseModel):
    id:int;actor_id:int|None;target_type:str;target_id:int;action:str;outcome:str;context:dict;created_at:datetime
class Overview(BaseModel):
    accounts:int;learners:int;creators:int;administrators:int;suspended:int
    published_rooms:int;draft_rooms:int;theory_rooms:int;practical_rooms:int;published_paths:int
    active_labs:int;active_builders:int;failed_labs:int;expired_labs:int
    recent_issues:list[LabRecord]
ACTIVE=('pending','provisioning','running','stopping')

def audit(db,actor,target_type,target_id,action,outcome,context):
    db.add(AdminAudit(actor_id=actor,target_type=target_type,target_id=target_id,action=action,outcome=outcome,context=json.dumps(context,sort_keys=True),created_at=datetime.utcnow()))
def control(db):
    if db.scalar(select(AdminControl).where(AdminControl.id==1).with_for_update()) is None:raise HTTPException(503,'Administration migration is required')
def fresh_actor(db,user):
    old_version=user.token_version
    actor=db.scalar(select(User).where(User.id==user.id).with_for_update().execution_options(populate_existing=True))
    if actor is None or actor.role!='admin' or actor.suspended or actor.token_version!=old_version:raise HTTPException(403,'Administrator authorization changed; sign in again')
@contextmanager
def mutation(db,user,kind,target,action,reason):
    try:
        control(db);fresh_actor(db,user)
        context={'reason':reason};yield context
        audit(db,user.id,kind,target,action,'success',context);db.commit()
    except HTTPException as exc:
        db.rollback()
        # Durable denied outcome, without recording bodies, credentials or error text.
        audit(db,user.id,kind,target,action,'denied',{'reason':reason,'status':exc.status_code});db.commit();raise
    except Exception:
        db.rollback();raise HTTPException(503,'Administrative action failed; no change was committed') from None

def account_query():
    points=select(QuestionSolve.learner_id,func.sum(QuestionSolve.points_awarded).label('n')).group_by(QuestionSolve.learner_id).subquery()
    proof=exists(select(QuestionSolve.id).join(ChallengeQuestion,ChallengeQuestion.id==QuestionSolve.question_id).where(QuestionSolve.learner_id==RoomProgress.learner_id,ChallengeQuestion.room_id==RoomProgress.room_id))
    done=select(RoomProgress.learner_id,func.count().label('n')).where(RoomProgress.completed_at.is_not(None),proof).group_by(RoomProgress.learner_id).subquery()
    return select(User.id,User.username,User.display_name,User.role,User.suspended,User.created_at,func.coalesce(points.c.n,0).label('points'),func.coalesce(done.c.n,0).label('completed_rooms')).outerjoin(points,points.c.learner_id==User.id).outerjoin(done,done.c.learner_id==User.id)
def page_result(db,query,page,size,model):
    total=db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return Page[model](items=[model(**r) for r in db.execute(query.offset((page-1)*size).limit(size)).mappings()],total=total,page=page,page_size=size)
@router.get('/users',response_model=Page[Account])
def users(response:Response,q:str=Query('',max_length=100),page:int=Query(1,ge=1,le=1000),page_size:int=Query(20,ge=1,le=50),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';query=account_query()
    for term in q.lower().split():query=query.where(func.lower(User.username).contains(term,autoescape=True)|func.lower(func.coalesce(User.display_name,'')).contains(term,autoescape=True))
    return page_result(db,query.order_by(User.id),page,page_size,Account)
@router.get('/users/{user_id}',response_model=Account)
def user_detail(user_id:int,response:Response,db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';row=db.execute(account_query().where(User.id==user_id)).mappings().first()
    if row is None:raise HTTPException(404,'Account unavailable')
    return Account(**row)
def active_resources(db,uid):
    return any(db.scalar(select(m.id).where(getattr(m,owner)==uid,((m.status.in_(ACTIVE))|((m.status=='failed')&m.instance_id.is_not(None)))).limit(1)) is not None for m,owner in [(LabInstance,'learner_id'),(BuilderEnvironment,'creator_id')])
@router.patch('/users/{user_id}',response_model=ActionResult)
def change_user(user_id:int,data:AccountChange,user:User=Depends(require_role('admin')),db:Session=Depends(get_db)):
    changed=False
    with mutation(db,user,'user',user_id,'role-change' if data.role else 'suspend' if data.suspended else 'reactivate',data.reason) as context:
        target=db.scalar(select(User).where(User.id==user_id).with_for_update().execution_options(populate_existing=True))
        if target is None:raise HTTPException(404,'Account unavailable')
        if target.id==user.id:raise HTTPException(409,'Self-management of roles or suspension is not allowed')
        if target.username!=data.confirm_username:raise HTTPException(409,'Confirm the current target username')
        if (target.role,target.suspended)!=(data.expected_role,data.expected_suspended):raise HTTPException(409,'Account changed; refresh before retrying')
        role=data.role if data.role is not None else target.role;suspended=data.suspended if data.suspended is not None else target.suspended
        if target.role=='admin' and not target.suspended and (role!='admin' or suspended):
            if db.scalar(select(func.count()).select_from(User).where(User.role=='admin',User.suspended.is_(False)))<=1:raise HTTPException(409,'The last active administrator must be preserved')
        changed=(role,suspended)!=(target.role,target.suspended)
        if changed:
            if active_resources(db,target.id):raise HTTPException(409,'Account has active or unresolved lab resources. Use the normal owner cleanup workflow before changing access')
            context.update(old_role=target.role,new_role=role,old_suspended=target.suspended,new_suspended=suspended)
            target.creator_access=(role=='admin' and (target.role=='creator' or target.creator_access))
            context['creator_access_retained']=target.creator_access
            target.role=role;target.suspended=suspended;target.token_version+=1
        context['changed']=changed
    return ActionResult(changed=changed)

def content_query(kind):
    model=Room if kind=='rooms' else LearningPath;title=model.name if kind=='rooms' else model.title
    cols=[model.id,title.label('title'),model.creator_id,User.username.label('creator'),model.category,model.difficulty,model.status,model.moderation_hold,model.created_at,model.updated_at]
    if kind=='rooms':cols.append(Room.room_type)
    return select(*cols).join(User,User.id==model.creator_id),title
@router.get('/content/{kind}',response_model=Page[Content])
def content(kind:Literal['rooms','paths'],response:Response,q:str=Query('',max_length=100),page:int=Query(1,ge=1,le=1000),page_size:int=Query(20,ge=1,le=50),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';query,title=content_query(kind)
    for term in q.lower().split():query=query.where(func.lower(title).contains(term,autoescape=True))
    return page_result(db,query.order_by(title,Room.id if kind=='rooms' else LearningPath.id),page,page_size,Content)
@router.post('/content/{kind}/{content_id}/moderate',response_model=ActionResult)
def moderate(kind:Literal['rooms','paths'],content_id:int,data:Moderation,user:User=Depends(require_role('admin')),db:Session=Depends(get_db)):
    with mutation(db,user,kind,content_id,'publish' if data.publish else 'unpublish',data.reason) as context:
        model=Room if kind=='rooms' else LearningPath
        item=db.scalar(select(model).where(model.id==content_id).with_for_update().execution_options(populate_existing=True))
        if item is None:raise HTTPException(404,'Content unavailable')
        if data.confirmation!=('PUBLISH ' if data.publish else 'UNPUBLISH ')+str(content_id):raise HTTPException(409,'Explicit content confirmation required')
        before=(item.status,item.moderation_hold)
        if data.publish:
            if kind=='rooms':
                template=validate_room(db,item,data.template_id,True)
                if template:
                    db.execute(update(RoomTemplate).where(RoomTemplate.room_id==item.id).values(is_published=False));db.flush();template.is_published=True
            else:
                if data.template_id is not None:raise HTTPException(422,'Paths do not use templates')
                validate_path(db,item,True)
            item.status='published';item.moderation_hold=False
            if kind=='rooms':
                from app.models import RoomRelease
                from app.room_workspace import document
                db.add(RoomRelease(room_id=item.id,document=document(item,db),image_template_id=template.id if template else None,published_by=user.id))
        else:
            item.status='draft';item.moderation_hold=True
            if kind=='rooms':db.execute(update(RoomTemplate).where(RoomTemplate.room_id==item.id).values(is_published=False))
        item.updated_at=datetime.utcnow();context.update(old_status=before[0],new_status=item.status,changed=before!=(item.status,item.moderation_hold))
    return ActionResult(changed=context['changed'])

def lab_query(kind):
    model=LabInstance if kind=='learner' else BuilderEnvironment;owner=model.learner_id if kind=='learner' else model.creator_id
    return select(model.id,owner.label('owner_id'),User.username.label('owner'),model.room_id,Room.name.label('room'),model.status,model.created_at.label('started_at'),model.expires_at,model.stopped_at,model.error_message).join(User,User.id==owner).join(Room,Room.id==model.room_id)
def lab_record(row,kind,now):
    d=dict(row);error=d.pop('error_message');stopped=d.pop('stopped_at');d['expires_at']=as_utc(d['expires_at']);d['started_at']=as_utc(d['started_at']);d.update(kind=kind,remaining_seconds=max(0,int((d['expires_at']-now).total_seconds())) if d['status'] in ACTIVE else 0)
    d['cleanup_status']='complete' if d['status']=='stopped' and stopped else 'retry pending' if error and d['status'] in ('failed','stopping') else 'in progress' if d['status']=='stopping' else 'not requested'
    d['issue']='Lifecycle operation failed; inspect private server diagnostics' if error else None
    return LabRecord(**d)
@router.get('/labs',response_model=LabPage)
def labs(response:Response,kind:Literal['learner','builder']='learner',status:Literal['all','active','failed','stopped']='active',page:int=Query(1,ge=1,le=1000),page_size:int=Query(20,ge=1,le=50),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';model=LabInstance if kind=='learner' else BuilderEnvironment;query=lab_query(kind)
    if status!='all':query=query.where(model.status.in_(ACTIVE) if status=='active' else model.status==status)
    total=db.scalar(select(func.count()).select_from(query.subquery()));now=datetime.now(timezone.utc)
    rows=db.execute(query.order_by(model.id.desc()).offset((page-1)*page_size).limit(page_size)).mappings()
    return LabPage(items=[lab_record(r,kind,now) for r in rows],total=total,page=page,page_size=page_size,server_time=now)
@router.get('',response_model=Overview)
def overview(response:Response,db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    def counts(model,definitions):
        return dict(db.execute(select(*[func.coalesce(func.sum(case((condition,1),else_=0)),0).label(key) for key,condition in definitions.items()])).mappings().one())
    values=counts(User,{'accounts':User.id.is_not(None),'learners':User.role=='learner','creators':User.role=='creator','administrators':User.role=='admin','suspended':User.suspended.is_(True)})
    values.update(counts(Room,{'published_rooms':Room.status=='published','draft_rooms':Room.status=='draft','theory_rooms':Room.room_type=='THEORY','practical_rooms':Room.room_type=='PRACTICAL'}))
    values.update(counts(LabInstance,{'active_labs':LabInstance.status.in_(ACTIVE),'failed_labs':LabInstance.status=='failed','expired_labs':LabInstance.expired_at.is_not(None)}))
    values.update(counts(BuilderEnvironment,{'active_builders':BuilderEnvironment.status.in_(ACTIVE)}))
    values.update(counts(LearningPath,{'published_paths':LearningPath.status=='published'}))
    now=datetime.now(timezone.utc);issues=[]
    for model,kind in [(LabInstance,'learner'),(BuilderEnvironment,'builder')]:
        issues.extend(lab_record(r,kind,now) for r in db.execute(lab_query(kind).where(model.error_message.is_not(None),model.status.in_(('failed','stopping'))).order_by(model.id.desc()).limit(5)).mappings())
    return Overview(**values,recent_issues=sorted(issues,key=lambda r:r.started_at,reverse=True)[:10])

@router.get('/audit',response_model=Page[AuditRecord])
def audit_log(response:Response,page:int=Query(1,ge=1,le=1000),page_size:int=Query(20,ge=1,le=50),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';total=db.scalar(select(func.count()).select_from(AdminAudit));rows=db.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).offset((page-1)*page_size).limit(page_size))
    return Page[AuditRecord](items=[AuditRecord(id=r.id,actor_id=r.actor_id,target_type=r.target_type,target_id=r.target_id,action=r.action,outcome=r.outcome,context=json.loads(r.context),created_at=as_utc(r.created_at)) for r in rows],total=total,page=page,page_size=page_size)
