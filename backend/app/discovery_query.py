"""Bounded literal metadata search and deterministic ordering, shared by catalogs."""
from typing import Literal
from sqlalchemy import or_,func,case,select
from app.models import Room,RoomCategory
from fastapi import HTTPException
from app.categories import CATEGORIES,SLUGS
Sort=Literal['newest','alphabetical','difficulty']
ProgressFilter=Literal['all','not-started','in-progress','completed']
def search_conditions(q,model,title):
    terms=list(dict.fromkeys(' '.join(q.lower().split()).split()))
    result=[]
    for term in terms:
        matching=[c['slug'] for c in CATEGORIES if term in c['label'].lower() or term in c['slug']]
        conditions=[*(func.lower(c).contains(term,autoescape=True) for c in (title,model.description,model.category)),model.category.in_(matching)]
        if model is Room:conditions.append(select(RoomCategory.room_id).where(RoomCategory.room_id==Room.id,RoomCategory.category.in_(matching)).exists())
        result.append(or_(*conditions))
    return result
def filters(q,category,difficulty,model,title):
    result=[model.status=='published',*search_conditions(q,model,title)]
    if category:
        values=category.split(',') if model is Room else [category]
        if len(values)>8 or any(v not in SLUGS for v in values):raise HTTPException(422,'Choose up to eight supported category slugs')
        condition=model.category.in_(values)
        if model is Room:condition=or_(condition,select(RoomCategory.room_id).where(RoomCategory.room_id==Room.id,RoomCategory.category.in_(values)).exists())
        result.append(condition)
    if difficulty:result.append(model.difficulty==difficulty)
    return result
def ordering(model,title,sort):
    if sort=='alphabetical':return (func.lower(title),model.id)
    if sort=='difficulty':return (case((model.difficulty=='easy',0),(model.difficulty=='medium',1),else_=2),model.id.desc())
    return (model.created_at.desc(),model.id.desc())
