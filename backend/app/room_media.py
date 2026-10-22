"""Private lesson images, safe decoding and explicit publication eligibility."""
import hashlib
import asyncio
import io
import os
import re
import secrets
import shutil
import stat
import time
import warnings
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from markdown_it import MarkdownIt
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import account_allowed, get_current_user
from app.avatars import LOCK, picture_guard, storage as avatar_storage
from app.challenges import ConfidentialValidationRoute
from app.database import get_db
from app.models import Room, RoomDraft, RoomMedia, RoomRelease, RoomReleaseMedia, User
from app.permissions import has_creator_access

MAX_BYTES = 2 * 1024 * 1024
MAX_OUTPUT = 2621440
MAX_DISK = 512 * 1024 * 1024
MAX_OWNER_IMAGES = 100
MAX_IMAGES = 5000
FORMATS = {'image/png': ('PNG', '.png'), 'image/jpeg': ('JPEG', '.jpg'), 'image/webp': ('WEBP', '.webp')}
KEY = re.compile(r'^[a-f0-9]{32}$')
SOURCE = re.compile(r'^/api/rooms/([1-9][0-9]*)/media/([a-f0-9]{32})$')
PARSER = MarkdownIt('commonmark', {'html': False, 'maxNesting': 32})
router = APIRouter(prefix='/rooms', tags=['Lesson images'], route_class=ConfidentialValidationRoute)


class MediaResponse(BaseModel):
    id: str
    room_id: int
    url: str
    media_type: str
    width: int
    height: int
    byte_size: int
    created_at: datetime
    in_use: bool = False
    model_config = ConfigDict(extra='forbid')


class MediaPage(BaseModel):
    items: list[MediaResponse]
    total: int
    page: int
    page_size: int


def private(response):
    response.headers.update({'Cache-Control': 'private, no-store', 'Vary': 'Authorization'})


def metadata(item, in_use=False):
    return MediaResponse(id=item.id, room_id=item.room_id, url=f'/api/rooms/{item.room_id}/media/{item.id}',
                         media_type=item.media_type, width=item.width, height=item.height,
                         byte_size=item.byte_size, created_at=item.created_at, in_use=in_use)


def owned(room_id, user, db):
    version = user.token_version
    fresh = db.scalar(select(User).where(User.id == user.id).with_for_update(read=True).execution_options(populate_existing=True))
    if not fresh or not account_allowed(fresh) or fresh.must_change_password or fresh.token_version != version:
        raise HTTPException(401, 'Invalid or expired token')
    if not has_creator_access(fresh):
        raise HTTPException(403, 'Creator access required')
    room = db.scalar(select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True))
    if room is None or room.creator_id != fresh.id:
        raise HTTPException(404, 'Room not found')
    return room


def image_ids(document, room_id, strict=False):
    ids = set()
    for task in document.get('tasks', []):
        stack = list(PARSER.parse(task.get('instructions', '')[:12000]))
        while stack:
            token = stack.pop()
            stack.extend(token.children or [])
            if token.type != 'image':
                continue
            match = SOURCE.fullmatch(token.attrGet('src') or '')
            if not match or int(match[1]) != room_id or not 1 <= len(token.content.strip()) <= 240:
                if strict:
                    raise HTTPException(422, 'Use this room’s uploaded images with descriptive alt text (1–240 characters).')
                continue
            ids.add(match[2])
    if len(ids) > MAX_OWNER_IMAGES:
        raise HTTPException(422, 'A room can reference at most 100 images.')
    return ids


def register_release_media(db, release, document):
    """Called inside the existing publication transaction, never commits."""
    ids = image_ids(document, release.room_id, strict=True)
    if not ids:
        return
    available = set(db.scalars(select(RoomMedia.id).where(RoomMedia.room_id == release.room_id, RoomMedia.id.in_(ids))))
    if ids != available:
        raise HTTPException(422, 'An instruction image is unavailable. Upload it to this room before publishing.')
    db.flush()
    db.add_all(RoomReleaseMedia(release_id=release.id, media_id=key) for key in sorted(ids))


def storage():
    root = avatar_storage() / 'room-media'
    if root.is_symlink():
        raise HTTPException(503, 'Lesson image storage unavailable')
    root.mkdir(mode=0o700, exist_ok=True)
    item = root.stat()
    if item.st_uid != os.getuid() or item.st_mode & 0o077:
        raise HTTPException(503, 'Lesson image storage unavailable')
    return root


def path_for(root, item):
    if not KEY.fullmatch(item.id) or item.media_type not in FORMATS:
        raise HTTPException(404, 'Image unavailable')
    return root / (item.id + FORMATS[item.media_type][1])


def decode_image(data, mime):
    if mime not in FORMATS:
        raise HTTPException(415, 'Use PNG, JPEG or WebP')
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, 'Lesson images must be at most 2 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[v[0] for v in FORMATS.values()]) as original:
                if original.format != FORMATS[mime][0] or getattr(original, 'is_animated', False):
                    raise ValueError()
                if min(original.size) < 16 or max(original.size) > 4096 or original.width * original.height > 12000000:
                    raise ValueError()
                original.verify()
            with Image.open(io.BytesIO(data), formats=[FORMATS[mime][0]]) as original:
                original.load()
                picture = ImageOps.exif_transpose(original).convert('RGB' if mime == 'image/jpeg' else 'RGBA')
                picture.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                clean = Image.new(picture.mode, picture.size)
                clean.paste(picture)
                output = io.BytesIO()
                options = {'compress_level': 6} if mime == 'image/png' else {'quality': 85}
                clean.save(output, format=FORMATS[mime][0], **options)
                body = output.getvalue()
                if len(body) > MAX_OUTPUT:
                    raise ValueError()
                return body, clean.width, clean.height
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise HTTPException(422, 'Use a valid non-animated image, 16–4096 pixels per side and at most 12 megapixels. Resize complex images before uploading.') from None


def sweep_orphan_files(root, db):
    """Only old decoder-named files with no database identity; saved drafts stay."""
    known = set(db.scalars(select(RoomMedia.id)))
    for p in root.iterdir():
        if not KEY.fullmatch(p.stem) or p.suffix not in {'.png', '.jpg', '.webp'} or p.stem in known:
            continue
        info = p.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and time.time() - info.st_mtime > 7 * 86400:
            p.unlink()


def used_ids(db, room_id):
    historical = set(db.scalars(select(RoomReleaseMedia.media_id).join(RoomMedia).where(RoomMedia.room_id == room_id)))
    draft = db.get(RoomDraft, room_id)
    return historical | (image_ids(draft.document, room_id) if draft else set())


def save_image(data, mime, room_id, user, db):
    with LOCK, picture_guard():
        owned(room_id, user, db)
        body, width, height = decode_image(data, mime)
        digest = hashlib.sha256(body).hexdigest()
        existing = db.scalar(select(RoomMedia).where(RoomMedia.room_id == room_id, RoomMedia.content_digest == digest))
        if existing:
            result = metadata(existing, existing.id in used_ids(db, room_id))
            db.commit()
            return result
        root = storage()
        sweep_orphan_files(root, db)
        owner_count = db.scalar(select(func.count()).select_from(RoomMedia).join(Room).where(Room.creator_id == user.id))
        total_count = db.scalar(select(func.count()).select_from(RoomMedia))
        if owner_count >= MAX_OWNER_IMAGES or total_count >= MAX_IMAGES:
            raise HTTPException(507, 'Image limit reached. Remove unused uploads before adding more.')
        usage = sum(p.lstat().st_size for p in root.iterdir())
        if usage + len(body) > MAX_DISK or shutil.disk_usage(root).free < 512 * 1024 * 1024:
            raise HTTPException(507, 'Lesson image storage is full')
        item = RoomMedia(id=secrets.token_hex(16), room_id=room_id, content_digest=digest,
                         media_type=mime, width=width, height=height, byte_size=len(body))
        path = path_for(root, item)
        commit_started = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            db.add(item)
            db.flush()
            commit_started = True
            db.commit()
            return metadata(item)
        except Exception:
            db.rollback()
            if not commit_started:
                path.unlink(missing_ok=True)
            raise


@router.post('/{room_id}/media', response_model=MediaResponse, status_code=201)
async def upload(room_id: int, request: Request, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Check ownership before accepting or decoding a body; do not hold row locks
    # across a slow HTTP stream. Revalidate under locks before touching storage.
    if not has_creator_access(user):
        raise HTTPException(403, 'Creator access required')
    room = db.get(Room, room_id)
    if room is None or room.creator_id != user.id:
        raise HTTPException(404, 'Room not found')
    mime = request.headers.get('content-type', '').split(';')[0].strip().lower()
    if mime not in FORMATS:
        raise HTTPException(415, 'Use PNG, JPEG or WebP')
    declared = request.headers.get('content-length')
    if declared:
        try:
            if not 0 <= int(declared) <= MAX_BYTES:
                raise HTTPException(413, 'Lesson images must be at most 2 MB')
        except ValueError:
            raise HTTPException(400, 'Invalid upload length') from None
    body = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BYTES:
                    raise HTTPException(413, 'Lesson images must be at most 2 MB')
                body.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, 'Image upload timed out; try again') from None
    private(response)
    try:
        return await run_in_threadpool(save_image, bytes(body), mime, room_id, user, db)
    except OSError:
        raise HTTPException(503, 'Lesson image storage unavailable') from None


@router.get('/{room_id}/media', response_model=MediaPage)
def listing(room_id: int, response: Response, page: int = Query(1, ge=1, le=1000), page_size: int = Query(12, ge=1, le=24), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owned(room_id, user, db)
    private(response)
    query = select(RoomMedia).where(RoomMedia.room_id == room_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(RoomMedia.created_at.desc(), RoomMedia.id).offset((page - 1) * page_size).limit(page_size))
    used = used_ids(db, room_id)
    return MediaPage(items=[metadata(i, i.id in used) for i in rows], total=total, page=page, page_size=page_size)


@router.get('/{room_id}/media/{key}')
def download(room_id: int, key: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not KEY.fullmatch(key):
        raise HTTPException(404, 'Image unavailable')
    room = db.get(Room, room_id)
    item = db.get(RoomMedia, key)
    if not room or not item or item.room_id != room.id:
        raise HTTPException(404, 'Image unavailable')
    if room.creator_id != user.id:
        if room.status != 'published':
            raise HTTPException(404, 'Image unavailable')
        latest = select(func.max(RoomRelease.id)).where(RoomRelease.room_id == room.id).scalar_subquery()
        if not db.scalar(select(RoomReleaseMedia.media_id).where(RoomReleaseMedia.release_id == latest, RoomReleaseMedia.media_id == item.id)):
            raise HTTPException(404, 'Image unavailable')
    try:
        fd = os.open(path_for(storage(), item), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size != item.byte_size or info.st_size > MAX_OUTPUT:
                raise HTTPException(404, 'Image unavailable')
            data = stream.read(MAX_OUTPUT + 1)
        if hashlib.sha256(data).hexdigest() != item.content_digest:
            raise HTTPException(404, 'Image unavailable')
    except OSError:
        raise HTTPException(404, 'Image unavailable') from None
    return Response(data, media_type=item.media_type, headers={'Cache-Control': 'private, no-store', 'Vary': 'Authorization',
                    'X-Content-Type-Options': 'nosniff', 'X-Image-Width': str(item.width), 'X-Image-Height': str(item.height)})


@router.delete('/{room_id}/media/{key}', status_code=204)
def remove(room_id: int, key: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with LOCK, picture_guard():
        owned(room_id, user, db)
        if not KEY.fullmatch(key):
            raise HTTPException(404, 'Image unavailable')
        item = db.get(RoomMedia, key)
        if not item or item.room_id != room_id:
            raise HTTPException(404, 'Image unavailable')
        if item.id in used_ids(db, room_id):
            raise HTTPException(409, 'This image is used in a saved draft or release and cannot be removed.')
        path = path_for(storage(), item)
        db.delete(item)
        db.commit()
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Old, unreferenced files are swept safely on a later upload.
            pass
    return Response(status_code=204, headers={'Cache-Control': 'private, no-store'})
