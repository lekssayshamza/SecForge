"""Shared publication validation. No owner impersonation, AWS calls or commits."""
from fastapi import HTTPException
from sqlalchemy import select
from app.models import Room,RoomTemplate,MachineTemplate,LearningPathRoom

def validate_room(db,room,template_id=None,moderation=False,updating=False):
    if room.status!='draft' and not (updating and room.status=='published'):raise HTTPException(409,'Only draft rooms can be published')
    if room.moderation_hold and not moderation:raise HTTPException(409,'Publication is held for administrator review')
    if room.room_type=='THEORY':
        if template_id is not None:raise HTTPException(422,'Theory rooms do not use saved images')
        return None
    if template_id is None:raise HTTPException(409,'Select a ready saved template version for practical publication')
    template=db.scalar(select(RoomTemplate).where(RoomTemplate.id==template_id,RoomTemplate.room_id==room.id).with_for_update())
    if not template:raise HTTPException(404,'Template not found')
    machine=db.scalar(select(MachineTemplate).where(MachineTemplate.room_template_id==template.id,MachineTemplate.role=='primary').order_by(MachineTemplate.id).limit(1))
    if not machine or machine.status!='available' or not machine.ami_id or not machine.ami_id.strip():raise HTTPException(409,'Template is not ready to be published')
    return template

def validate_path(db,path,moderation=False):
    if path.moderation_hold and not moderation:raise HTTPException(409,'Publication is held for administrator review')
    ids=select(LearningPathRoom.room_id).where(LearningPathRoom.path_id==path.id)
    rooms=list(db.scalars(select(Room).where(Room.id.in_(ids)).order_by(Room.id).with_for_update().execution_options(populate_existing=True)))
    if not rooms or any(r.status!='published' for r in rooms):raise HTTPException(409,'A published path needs at least one room and every room must be published')
