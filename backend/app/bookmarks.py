"""Private idempotent learner associations, never content ownership or scoring."""
from datetime import datetime
from typing import Literal,Annotated
from fastapi import APIRouter,Depends,HTTPException,Path,Query,Response
from pydantic import BaseModel
from sqlalchemy import select,delete,func
from sqlalchemy.orm import Session
from app.permissions import has_learning_access
from app.auth import require_role
from app.database import get_db
from app.models import User,Room,LearningPath,RoomBookmark,PathBookmark
from app.challenges import transaction
from app.discovery import RoomPage,LearnerPathPage,room_page,path_page
router=APIRouter(tags=['Saved content'])
Kind=Literal['rooms','paths']
Target=Annotated[int,Path(ge=1,le=2147483647)]
class BookmarkState(BaseModel):bookmarked:bool

def tables(kind):return (Room,RoomBookmark,'room_id') if kind=='rooms' else (LearningPath,PathBookmark,'path_id')
def published_target(kind,target_id,db,lock=False):
    model,_,_=tables(kind);query=select(model.id).where(model.id==target_id,model.status=='published')
    if lock:query=query.with_for_update()
    if db.scalar(query) is None:raise HTTPException(404,'Content unavailable')
def state(kind,target_id,user,db):
    _,bookmark,key=tables(kind)
    return db.scalar(select(bookmark.user_id).where(bookmark.user_id==user.id,getattr(bookmark,key)==target_id)) is not None
@router.get('/bookmarks/{kind}/{target_id}',response_model=BookmarkState)
def read(kind:Kind,target_id:Target,response:Response,user:User=Depends(require_role('learner')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';published_target(kind,target_id,db);return BookmarkState(bookmarked=state(kind,target_id,user,db))
def change(kind,target_id,user,db,add):
    with transaction(db):
        locked=db.scalar(select(User).where(User.id==user.id).with_for_update().execution_options(populate_existing=True))
        if locked is None or not has_learning_access(locked) or locked.suspended or locked.token_version != user.token_version:raise HTTPException(403,'Learner access required')
        _,bookmark,key=tables(kind);condition=[bookmark.user_id==user.id,getattr(bookmark,key)==target_id]
        if add:
            published_target(kind,target_id,db,True)
            if not state(kind,target_id,user,db):
                if db.scalar(select(func.count()).select_from(bookmark).where(bookmark.user_id==user.id))>=1000:raise HTTPException(409,'Saved content limit reached; remove a bookmark first')
                db.add(bookmark(**{'user_id':user.id,key:target_id,'created_at':datetime.utcnow()}));db.flush()
        else:db.execute(delete(bookmark).where(*condition))
    return BookmarkState(bookmarked=add)
@router.put('/bookmarks/{kind}/{target_id}',response_model=BookmarkState)
def add(kind:Kind,target_id:Target,response:Response,user:User=Depends(require_role('learner')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';return change(kind,target_id,user,db,True)
@router.delete('/bookmarks/{kind}/{target_id}',response_model=BookmarkState)
def remove(kind:Kind,target_id:Target,response:Response,user:User=Depends(require_role('learner')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';return change(kind,target_id,user,db,False)
@router.get('/saved/rooms',response_model=RoomPage)
def saved_rooms(response:Response,q:str=Query('',max_length=100),page:int=Query(1,ge=1,le=1000),page_size:int=Query(24,ge=1,le=48),user:User=Depends(require_role('learner')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';return room_page(db,q=q,page=page,page_size=page_size,uid=user.id,saved=True)
@router.get('/saved/paths',response_model=LearnerPathPage)
def saved_paths(response:Response,q:str=Query('',max_length=100),page:int=Query(1,ge=1,le=1000),page_size:int=Query(24,ge=1,le=48),user:User=Depends(require_role('learner')),db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store';return path_page(db,q=q,page=page,page_size=page_size,uid=user.id,saved=True)
