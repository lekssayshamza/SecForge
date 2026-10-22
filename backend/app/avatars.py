"""Private, bounded avatar storage. Client filenames/paths are never used."""
import io,logging,os,re,stat,threading,uuid,warnings,time,fcntl
from contextlib import contextmanager
from pathlib import Path
from PIL import Image,ImageOps,UnidentifiedImageError
from fastapi import APIRouter,Depends,HTTPException,Request,Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import UserResponse
router=APIRouter(prefix='/users/me/avatar',tags=['Profile'])
MAX_LOCK_WAIT=5
MAX_BYTES=2*1024*1024;MAX_DISK=512*1024*1024;MAX_OUTPUT=300*1024
FORMATS={'image/jpeg':'JPEG','image/png':'PNG','image/webp':'WEBP'}
LOCK=threading.Lock();KEY=re.compile(r'^[a-f0-9]{32}$');log=logging.getLogger(__name__)

def storage():
    root=Path(settings.avatar_storage_dir)
    if not root.is_absolute() or root.is_symlink() or root.resolve()!=root:raise HTTPException(503,'Picture storage unavailable')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    s=root.stat()
    if s.st_uid!=os.getuid() or s.st_mode&0o077:raise HTTPException(503,'Picture storage unavailable')
    return root

@contextmanager
def picture_guard():
    # The daily backup holds this same lock around pg_dump + avatar archive.
    root=storage();fd=os.open(root/'.backup.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        item=os.fstat(fd)
        if not stat.S_ISREG(item.st_mode) or item.st_uid!=os.getuid() or item.st_mode&0o077:raise HTTPException(503,'Picture storage unavailable')
        # Bounded wait; a backup must never exhaust web workers indefinitely.
        deadline=time.monotonic()+MAX_LOCK_WAIT
        while True:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise HTTPException(503,'Picture storage is busy; try again shortly') from None
                time.sleep(.05)
        yield
    finally:os.close(fd)

def picture_path(root,key):
    if not isinstance(key,str) or not KEY.fullmatch(key):raise HTTPException(404,'Picture unavailable')
    return root/(key+'.png')

def decode_picture(data,mime):
    if mime not in FORMATS:raise HTTPException(415,'Use JPEG, PNG or WebP')
    if not data or len(data)>MAX_BYTES:raise HTTPException(413,'Picture must be at most 2 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data),formats=list(FORMATS.values())) as image:
                if image.format!=FORMATS[mime] or getattr(image,'is_animated',False):raise ValueError()
                if min(image.size)<16 or max(image.size)>2048:raise ValueError()
                image.verify()
            with Image.open(io.BytesIO(data),formats=list(FORMATS.values())) as image:
                image.load();image=ImageOps.exif_transpose(image).convert('RGBA')
                image=ImageOps.fit(image,(256,256),method=Image.Resampling.LANCZOS)
                clean=Image.new('RGBA',(256,256));clean.paste(image)
                output=io.BytesIO();clean.save(output,format='PNG',compress_level=6)
                result=output.getvalue()
                if len(result)>MAX_OUTPUT:raise ValueError()
                return result
    except (ValueError,OSError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise HTTPException(422,'Use a valid, non-animated JPEG, PNG or WebP between 16 and 2048 pixels per side') from None

def unlink_owned(root,key):
    if not key:return
    path=picture_path(root,key)
    try:
        s=path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid():return
        path.unlink()
    except FileNotFoundError:pass
    except OSError:log.warning('Obsolete picture cleanup deferred')

def sweep_owned(root,db):
    """Only decoder-generated names, unreferenced and older than a day."""
    referenced=set(db.scalars(select(User.avatar_version).where(User.avatar_version.is_not(None))))
    for p in root.iterdir():
        if p.suffix!='.png' or not KEY.fullmatch(p.stem) or p.stem in referenced:continue
        s=p.lstat()
        if stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and time.time()-s.st_mtime>86400:unlink_owned(root,p.stem)

def save_picture(data,mime,uid,version,db):
    with LOCK,picture_guard():
        body=decode_picture(data,mime);root=storage();sweep_owned(root,db)
        user=db.scalar(select(User).where(User.id==uid).with_for_update().execution_options(populate_existing=True))
        if not user or user.token_version!=version or user.must_change_password:raise HTTPException(401,'Invalid or expired token')
        # Count all files; unexpected entries are never deleted to make space.
        usage=sum(p.lstat().st_size for p in root.iterdir())
        if usage+len(body)>MAX_DISK:raise HTTPException(507,'Picture storage is full; try again later')
        old=user.avatar_version;key=uuid.uuid4().hex;path=picture_path(root,key)
        commit_started=False
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as file:file.write(body);file.flush();os.fsync(file.fileno())
            directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
            user.avatar_version=key;commit_started=True;db.commit();db.refresh(user)
        except Exception:
            db.rollback()
            # Commit acknowledgment can be lost: preserve recoverable files.
            if not commit_started:unlink_owned(root,key)
            raise
        unlink_owned(root,old);return user

@router.put('',response_model=UserResponse)
async def upload_avatar(request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    mime=request.headers.get('content-type','').split(';')[0].lower()
    if mime not in FORMATS:raise HTTPException(415,'Use JPEG, PNG or WebP')
    declared=request.headers.get('content-length')
    if declared:
        try:
            if int(declared)>MAX_BYTES or int(declared)<0:raise HTTPException(413,'Picture must be at most 2 MB')
        except ValueError:raise HTTPException(400,'Invalid upload length') from None
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>MAX_BYTES:raise HTTPException(413,'Picture must be at most 2 MB')
        body.extend(chunk)
    try:return await run_in_threadpool(save_picture,bytes(body),mime,user.id,user.token_version,db)
    except OSError:raise HTTPException(503,'Picture storage unavailable; try again later') from None

@router.get('')
def get_avatar(user:User=Depends(get_current_user)):
    if not user.avatar_version:raise HTTPException(404,'No uploaded picture')
    try:
        path=picture_path(storage(),user.avatar_version);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as file:
            s=os.fstat(file.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_size>MAX_OUTPUT:raise HTTPException(404,'Picture unavailable')
            data=file.read(MAX_OUTPUT+1)
        return Response(data,media_type='image/png',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Vary':'Authorization'})
    except OSError:raise HTTPException(404,'Picture unavailable') from None

@router.delete('',response_model=UserResponse)
def remove_avatar(user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    try:return delete_picture(user,db)
    except OSError:raise HTTPException(503,'Picture storage unavailable; try again later') from None

def delete_picture(user,db):
    with LOCK,picture_guard():
        root=storage();uid=user.id;version=user.token_version
        fresh=db.scalar(select(User).where(User.id==uid).with_for_update().execution_options(populate_existing=True))
        if not fresh or fresh.token_version!=version:raise HTTPException(401,'Invalid or expired token')
        old=fresh.avatar_version;fresh.avatar_version=None;db.commit();db.refresh(fresh);unlink_owned(root,old);return fresh
