"""Room type is immutable after creation; machine routes always fail closed."""
from fastapi import Depends,HTTPException
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.database import get_db
from app.models import User,Room
from app.rooms import get_room_or_404

def require_practical_room(room_id:int,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    room=db.get(Room,room_id)
    # Practical/missing rooms retain the original endpoint's role, ownership and
    # error semantics. Theory is rejected before any machine side effects.
    if room is not None and room.room_type=='THEORY':
        get_room_or_404(room_id,user,db)
        raise HTTPException(409,'Theory rooms do not use machines, images or VPN connections')
    return room
