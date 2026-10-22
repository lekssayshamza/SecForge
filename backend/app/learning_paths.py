from app.permissions import has_creator_access, has_learning_access
"""Owned ordered collections; public visibility and progress are derived live."""
from datetime import datetime
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,Query,Response
from pydantic import BaseModel,Field,field_validator
from sqlalchemy import select,func,or_,delete
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.categories import validate_category, attach_categories
from app.database import get_db
from app.models import User,Room,RoomCategory,RoomProgress,LearningPath,LearningPathRoom,ChallengeQuestion,RoomDraft
from app.schemas import PublicRoomResponse
from app.public_rooms import FIELDS
from app.challenges import transaction,ConfidentialValidationRoute

router=APIRouter(prefix='/paths',tags=['Learning paths'],route_class=ConfidentialValidationRoute)
public_router=APIRouter(prefix='/public/paths',tags=['Public learning paths'])
class PathWrite(BaseModel):
    title:str=Field(min_length=1,max_length=120)
    description:str=Field(default='',max_length=2000)
    difficulty:Literal['easy','medium','hard']='easy'
    category:str=Field(min_length=1,max_length=50)
    objectives:str=Field(default='',max_length=2000)
    model_config={'extra':'forbid'}
    _category = field_validator('category')(validate_category)
    @field_validator('title')
    @classmethod
    def nonempty(cls,v):
        v=v.strip()
        if not v or any(ord(c)<32 for c in v):raise ValueError('Nonempty text without controls required')
        return v
class PathUpdate(PathWrite):
    room_ids: list[int] | None = Field(default=None, max_length=50)
class PathSummary(BaseModel):
    title:str;description:str;difficulty:str;category:str;objectives:str
    id:int
    status:Literal['draft','published']
    room_count:int
class PathPage(BaseModel):
    items:list[PathSummary];total:int;page:int;page_size:int;categories:list[str]
class PathDetail(PathSummary):
    rooms:list[PublicRoomResponse]
    unavailable_count:int=0
class CreatorPathRoom(PublicRoomResponse):
    publication_status:Literal['draft','published']
class OwnerPathDetail(PathDetail):
    rooms:list[CreatorPathRoom]
class Membership(BaseModel):
    room_ids:list[int]=Field(max_length=50)
    model_config={'extra':'forbid'}
class PathProgress(BaseModel):
    path_id:int;completed_room_ids:list[int];rooms_total:int;rooms_completed:int;percent:int;next_room_id:int|None;completed:bool
class EligiblePage(BaseModel):
    items:list[CreatorPathRoom];page:int;page_size:int;has_more:bool

def room_projection():
    points=select(func.sum(ChallengeQuestion.points)).where(ChallengeQuestion.room_id==Room.id,ChallengeQuestion.is_active).correlate(Room).scalar_subquery()
    return (*FIELDS,Room.status.label('publication_status'),func.coalesce(points,0).label('points_total'))

def creator_room_rows(db,rows,user=None):
    items=attach_categories(db,rows)
    # A creator's saved private draft can contain questions not yet in the live
    # read model. Project their point total only; never serialize grading data.
    if user is not None:
        ids=[r['id'] for r in items if r['publication_status']=='draft']
        drafts=dict(db.execute(select(RoomDraft.room_id,RoomDraft.document).join(Room,Room.id==RoomDraft.room_id).where(RoomDraft.room_id.in_(ids),Room.creator_id==user.id)).all()) if ids else {}
        for item in items:
            if item['id'] in drafts:item['points_total']=sum(q['points'] for q in drafts[item['id']].get('questions',[]))
    return items

def owner(user):
    if not has_creator_access(user):raise HTTPException(403,'Creator access required')
def owned(path_id,user,db):
    owner(user)
    p=db.scalar(select(LearningPath).where(LearningPath.id==path_id,LearningPath.creator_id==user.id).with_for_update().execution_options(populate_existing=True))
    if not p:raise HTTPException(404,'Path not found')
    return p

def visible(path_id,db,user=None):
    cond=Room.status=='published'
    if user is not None:cond=or_(cond,Room.creator_id==user.id)
    return db.execute(select(*room_projection()).join(LearningPathRoom,LearningPathRoom.room_id==Room.id).where(LearningPathRoom.path_id==path_id,cond).order_by(LearningPathRoom.display_order)).mappings().all()
def summary(p,count):return PathSummary(**{k:getattr(p,k) for k in ['id','title','description','difficulty','category','objectives','status']},room_count=count)
def detail(p,db,user=None):
    rooms=visible(p.id,db,user);total=db.scalar(select(func.count()).select_from(LearningPathRoom).where(LearningPathRoom.path_id==p.id))
    room_schema=CreatorPathRoom if user else PublicRoomResponse
    schema=OwnerPathDetail if user else PathDetail
    return schema(**summary(p,len(rooms)).model_dump(),rooms=[room_schema(**r) for r in creator_room_rows(db,rooms,user)],unavailable_count=total-len(rooms) if user else 0)
def published(path_id,db):
    p=db.scalar(select(LearningPath).where(LearningPath.id==path_id,LearningPath.status=='published'))
    if not p:raise HTTPException(404,'Path not found')
    return p

def listing(db,conditions,page,page_size):
    counts=select(LearningPathRoom.path_id,func.count().label('n')).join(Room,Room.id==LearningPathRoom.room_id).where(Room.status=='published').group_by(LearningPathRoom.path_id).subquery()
    total=db.scalar(select(func.count()).select_from(LearningPath).where(*conditions))
    rows=db.execute(select(LearningPath,func.coalesce(counts.c.n,0)).outerjoin(counts,counts.c.path_id==LearningPath.id).where(*conditions).order_by(LearningPath.id.desc()).offset((page-1)*page_size).limit(page_size)).all()
    categories=list(db.scalars(select(LearningPath.category).where(*conditions).distinct().order_by(LearningPath.category).limit(100)))
    return PathPage(items=[summary(p,n) for p,n in rows],total=total,page=page,page_size=page_size,categories=categories)

@public_router.get('',response_model=PathPage)
def browse(response:Response,q:str=Query('',max_length=100),category:str=Query('',max_length=50),difficulty:Literal['','easy','medium','hard']='',sort:Literal['newest','alphabetical','difficulty']='newest',page:int=Query(1,ge=1,le=1000),page_size:int=Query(24,ge=1,le=48),db:Session=Depends(get_db)):
    from app.discovery import path_page
    response.headers['Cache-Control']='no-store'
    return path_page(db,q=q,category=category,difficulty=difficulty,sort=sort,page=page,page_size=page_size)
@public_router.get('/{path_id}',response_model=PathDetail)
def read_public(path_id:int,response:Response,db:Session=Depends(get_db)):
    response.headers['Cache-Control']='no-store';return detail(published(path_id,db),db)

@router.get('',response_model=PathPage)
def mine(response:Response,page:int=Query(1,ge=1,le=1000),page_size:int=Query(24,ge=1,le=48),user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    owner(user);response.headers['Cache-Control']='private, no-store';return listing(db,[LearningPath.creator_id==user.id],page,page_size)
@router.get('/eligible-rooms',response_model=EligiblePage)
def eligible(response:Response,q:str=Query('',max_length=100),category:str=Query('',max_length=50),difficulty:Literal['','easy','medium','hard']='',page:int=Query(1,ge=1,le=1000),user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    owner(user);response.headers['Cache-Control']='private, no-store';cond=[or_(Room.status=='published',Room.creator_id==user.id)]
    from app.discovery_query import search_conditions
    cond.extend(search_conditions(q,Room,Room.name))
    if category:
        try:validate_category(category)
        except ValueError:raise HTTPException(422,'Choose a supported category') from None
        cond.append(or_(Room.category==category,select(RoomCategory.room_id).where(RoomCategory.room_id==Room.id,RoomCategory.category==category).exists()))
    if difficulty:cond.append(Room.difficulty==difficulty)
    cond.append(Room.status.in_(['draft','published']))
    rows=db.execute(select(*room_projection()).where(*cond).order_by(Room.id.desc()).offset((page-1)*24).limit(25)).mappings().all()
    return EligiblePage(items=[CreatorPathRoom(**r) for r in creator_room_rows(db,rows[:24],user)],page=page,page_size=24,has_more=len(rows)>24)
@router.post('',response_model=OwnerPathDetail,status_code=201)
def create(data:PathWrite,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    owner(user)
    with transaction(db):
        p=LearningPath(creator_id=user.id,**data.model_dump());db.add(p);db.flush();result=detail(p,db,user)
    return result
@router.get('/{path_id}',response_model=OwnerPathDetail)
def read_owned(path_id:int,response:Response,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';return detail(owned(path_id,user,db),db,user)

def replace_rooms(p,ids,user,db):
    if len(ids)!=len(set(ids)):raise HTTPException(422,'Each room may appear once')
    # Lock the path before rooms, and room IDs in ascending order.
    rooms=list(db.scalars(select(Room).where(Room.id.in_(ids)).order_by(Room.id).with_for_update().execution_options(populate_existing=True)))
    if len(rooms)!=len(ids) or any(r.status!='published' and r.creator_id!=user.id for r in rooms):raise HTTPException(422,'Select owned or published rooms only')
    db.execute(delete(LearningPathRoom).where(LearningPathRoom.path_id==p.id));db.flush()
    db.add_all([LearningPathRoom(path_id=p.id,room_id=r,display_order=i+1) for i,r in enumerate(ids)]);db.flush()

@router.patch('/{path_id}',response_model=OwnerPathDetail)
def edit(path_id:int,data:PathUpdate,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        p=owned(path_id,user,db)
        if p.status!='draft':raise HTTPException(409,'Unpublish the path before editing')
        for k,v in data.model_dump(exclude={'room_ids'}).items():setattr(p,k,v)
        if data.room_ids is not None:replace_rooms(p,data.room_ids,user,db)
        p.updated_at=datetime.utcnow();db.flush();result=detail(p,db,user)
    return result
@router.put('/{path_id}/rooms',response_model=OwnerPathDetail)
def membership(path_id:int,data:Membership,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if len(data.room_ids)!=len(set(data.room_ids)):raise HTTPException(422,'Each room may appear once')
    with transaction(db):
        p=owned(path_id,user,db)
        if p.status!='draft':raise HTTPException(409,'Unpublish the path before editing')
        replace_rooms(p,data.room_ids,user,db);p.updated_at=datetime.utcnow();result=detail(p,db,user)
    return result
@router.post('/{path_id}/publish',response_model=OwnerPathDetail)
def publish(path_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        p=owned(path_id,user,db)
        from app.publication import validate_path
        validate_path(db,p)
        ids=list(db.scalars(select(LearningPathRoom.room_id).where(LearningPathRoom.path_id==path_id)))
        rooms=list(db.scalars(select(Room).where(Room.id.in_(ids)).order_by(Room.id).with_for_update().execution_options(populate_existing=True)))
        if not rooms or any(r.status!='published' for r in rooms):raise HTTPException(409,'A published path needs at least one room and every room must be published')
        p.status='published';p.updated_at=datetime.utcnow();db.flush();result=detail(p,db,user)
    return result
@router.post('/{path_id}/unpublish',response_model=OwnerPathDetail)
def unpublish(path_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        p=owned(path_id,user,db);p.status='draft';p.updated_at=datetime.utcnow();db.flush();result=detail(p,db,user)
    return result
@router.delete('/{path_id}',status_code=204)
def remove(path_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    with transaction(db):
        p=owned(path_id,user,db)
        if p.status=='published':raise HTTPException(409,'Unpublish the path before deletion')
        db.delete(p)
@router.get('/{path_id}/progress',response_model=PathProgress)
def progress(path_id:int,response:Response,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if not has_learning_access(user):raise HTTPException(403,'Learner access required')
    response.headers['Cache-Control']='private, no-store';published(path_id,db);ids=[r['id'] for r in visible(path_id,db)]
    solved=set(db.scalars(select(RoomProgress.room_id).where(RoomProgress.learner_id==user.id,RoomProgress.room_id.in_(ids),RoomProgress.completed_at.is_not(None))))
    return PathProgress(path_id=path_id,completed_room_ids=[i for i in ids if i in solved],rooms_total=len(ids),rooms_completed=len(solved),percent=round(100*len(solved)/len(ids)) if ids else 0,next_room_id=next((i for i in ids if i not in solved),None),completed=bool(ids) and len(ids)==len(solved))
