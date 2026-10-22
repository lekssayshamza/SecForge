"""Protected presentation-only task sections; scoring stays in challenges."""
from datetime import datetime
import unicodedata
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field,field_validator
from sqlalchemy import select,update
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.database import get_db
from app.models import RoomTask,ChallengeQuestion,User
from app.challenges import owned_room,transaction,ConfidentialValidationRoute
from app.rooms import get_room_or_404
router=APIRouter(prefix='/rooms',tags=['Room tasks'],route_class=ConfidentialValidationRoute)
class TaskWrite(BaseModel):
    title:str=Field(min_length=1,max_length=120)
    instructions:str=Field(default='',max_length=12000)
    model_config={'extra':'forbid'}
    @field_validator('title','instructions')
    @classmethod
    def clean(cls,value,info):
        value=unicodedata.normalize('NFC',value.replace('\r\n','\n').strip())
        if any(unicodedata.category(c).startswith('C') and (info.field_name=='title' or c not in '\n\t') for c in value):raise ValueError('Unsupported control characters')
        if info.field_name=='title' and not value:raise ValueError('Task title is required')
        return value
class TaskResponse(BaseModel):
    id:int;title:str;instructions:str;display_order:int
    model_config={'from_attributes':True}
class TaskList(BaseModel):tasks:list[TaskResponse]
class TaskOrder(BaseModel):
    task_ids:list[int]=Field(min_length=1,max_length=30)
    model_config={'extra':'forbid'}
def rows(room_id,db):return list(db.scalars(select(RoomTask).where(RoomTask.room_id==room_id).order_by(RoomTask.display_order)))
def response(room_id,db):return TaskList(tasks=[TaskResponse.model_validate(t) for t in rows(room_id,db)])
def selected(room_id,task_id,db):
    task=db.scalar(select(RoomTask).where(RoomTask.id==task_id,RoomTask.room_id==room_id))
    if task is None:raise HTTPException(404,'Task not found')
    return task
def reorder(tasks,db):
    for i,t in enumerate(tasks):t.display_order=1000+i
    db.flush()
    for i,t in enumerate(tasks):t.display_order=i+1
    db.flush()
@router.get('/{room_id}/tasks',response_model=TaskList)
def list_tasks(room_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    get_room_or_404(room_id,user,db);return response(room_id,db)
@router.post('/{room_id}/tasks',response_model=TaskResponse,status_code=201)
def create_task(room_id:int,data:TaskWrite,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        owned_room(room_id,user,db);existing=rows(room_id,db)
        if len(existing)>=30:raise HTTPException(409,'A room may contain at most 30 tasks')
        task=RoomTask(room_id=room_id,title=data.title,instructions=data.instructions,display_order=len(existing)+1);db.add(task);db.flush();result=TaskResponse.model_validate(task)
    return result
@router.put('/{room_id}/tasks/order',response_model=TaskList)
def order_tasks(room_id:int,data:TaskOrder,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        owned_room(room_id,user,db);existing={t.id:t for t in rows(room_id,db)}
        if len(data.task_ids)!=len(set(data.task_ids)) or set(data.task_ids)!=set(existing):raise HTTPException(422,'Provide every task exactly once')
        reorder([existing[i] for i in data.task_ids],db);result=response(room_id,db)
    return result
@router.patch('/{room_id}/tasks/{task_id}',response_model=TaskResponse)
def edit_task(room_id:int,task_id:int,data:TaskWrite,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        owned_room(room_id,user,db);task=selected(room_id,task_id,db);task.title=data.title;task.instructions=data.instructions;db.flush();result=TaskResponse.model_validate(task)
    return result
@router.delete('/{room_id}/tasks/{task_id}',status_code=204)
def delete_task(room_id:int,task_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        owned_room(room_id,user,db);task=selected(room_id,task_id,db)
        # Preserve every question and solve, including inactive retained questions.
        db.execute(update(ChallengeQuestion).where(ChallengeQuestion.room_id==room_id,ChallengeQuestion.task_id==task_id).values(task_id=None));db.flush();db.delete(task);db.flush();reorder(rows(room_id,db),db)
