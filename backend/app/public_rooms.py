"""Read-only published briefings. No authenticated or infrastructure data joins."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Room, ChallengeQuestion
from app.schemas import PublicRoomPage, PublicRoomResponse

router = APIRouter(prefix='/public/rooms', tags=['Public rooms'])
# Select columns explicitly: adding fields to Room can never widen this API.
FIELDS = (Room.id, Room.name, Room.description, Room.category, Room.difficulty, Room.room_type, Room.objectives, Room.prerequisites)


@router.get('', response_model=PublicRoomPage)
def browse_rooms(
    response: Response,
    q: str = Query('', max_length=100),
    category: str = Query('', max_length=400),
    difficulty: Literal['', 'easy', 'medium', 'hard'] = '',
    room_type: Literal['', 'THEORY', 'PRACTICAL'] = '',
    sort: Literal['newest','alphabetical','difficulty'] = 'newest',
    progress: Literal['all'] = 'all',
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(24, ge=1, le=48),
    db: Session = Depends(get_db),
):
    from app.discovery import room_page
    response.headers['Cache-Control'] = 'no-store'
    return room_page(db,q=q,category=category,difficulty=difficulty,room_type=room_type,sort=sort,page=page,page_size=page_size)


@router.get('/{room_id}', response_model=PublicRoomResponse)
def public_room(response: Response, room_id: int = Path(ge=1, le=2147483647), db: Session = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    points = select(func.sum(ChallengeQuestion.points)).where(ChallengeQuestion.room_id == Room.id, ChallengeQuestion.is_active).scalar_subquery()
    row = db.execute(select(*FIELDS, func.coalesce(points, 0).label('points_total')).where(Room.id == room_id, Room.status == 'published')).mappings().first()
    if row is None:
        raise HTTPException(404, 'Room not found')
    from app.categories import attach_categories
    return PublicRoomResponse(**attach_categories(db, [row])[0])
