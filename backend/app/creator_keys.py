"""Personal public keys for creator builders; never a private-key download API."""
import base64
import hashlib
import subprocess
from datetime import datetime,timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519,rsa
from fastapi import APIRouter,Depends,HTTPException,Response,Path
from pydantic import BaseModel,Field,field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.auth import account_allowed,get_current_user,require_role
from app.challenges import ConfidentialValidationRoute
from app.config import settings
from app.database import get_db
from app.models import User,CreatorSshKey,CreatorBuilderKey,BuilderEnvironment,MachineTemplate
from app.permissions import has_creator_access
from app.room_workspace import owned,private_response
from app.ops_lock import single_flight,builder_lock_key,OperationInProgress
from app.creator_key_guest import program

router=APIRouter(tags=['Creator SSH keys'],route_class=ConfidentialValidationRoute,dependencies=[Depends(private_response)])


def normalize_key(value):
    if not isinstance(value,str) or '\n' in value.strip() or '\r' in value or len(value)>2048 or 'PRIVATE KEY' in value:raise ValueError('Paste an OpenSSH public key, never a private key')
    parts=value.strip().split()
    if len(parts)<2 or parts[0] not in ('ssh-ed25519','ssh-rsa'):raise ValueError('Use an Ed25519 or RSA public key')
    try:
        key=serialization.load_ssh_public_key((' '.join(parts[:2])).encode('ascii'))
        if not isinstance(key,(ed25519.Ed25519PublicKey,rsa.RSAPublicKey)) or isinstance(key,rsa.RSAPublicKey) and key.key_size<3072:raise ValueError()
        return key.public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode('ascii')
    except Exception:raise ValueError('Use a valid Ed25519 or RSA key of at least 3072 bits') from None


def fingerprint(value):return 'SHA256:'+base64.b64encode(hashlib.sha256(base64.b64decode(value.split()[1])).digest()).decode().rstrip('=')


class KeyWrite(BaseModel):
    public_key:str=Field(min_length=40,max_length=2048)
    revision:int=Field(ge=0,strict=True)
    model_config={'extra':'forbid'}
    _public=field_validator('public_key')(normalize_key)
class KeyRevision(BaseModel):
    revision:int=Field(ge=0,strict=True)
    model_config={'extra':'forbid'}
class AdminRevoke(KeyRevision):
    confirm_username:str=Field(min_length=3,max_length=50)
    reason:str=Field(pattern='^(owner-request|access-change|account-review)$')
class KeyResponse(BaseModel):
    configured:bool
    revision:int=0
    enabled:bool=False
    fingerprint:str|None=None
    public_key:str|None=None
    applied_builder_id:int|None=None
    applied_at:datetime|None=None
    pending:bool=False


def view(key,include_public=True):
    if not key:return KeyResponse(configured=False)
    return KeyResponse(configured=True,revision=key.revision,enabled=key.enabled,fingerprint=key.fingerprint,public_key=key.public_key if include_public else None,applied_builder_id=key.applied_builder_id,applied_at=key.applied_at,pending=key.pending)


def creator(user):
    if not has_creator_access(user):raise HTTPException(403,'Creator access required')


def fresh_user(db,user,admin=False,exclusive=False):
    version=user.token_version
    query=select(User).where(User.id==user.id)
    query=query.with_for_update() if exclusive else query.with_for_update(read=True)
    current=db.scalar(query.execution_options(populate_existing=True))
    if not current or not account_allowed(current) or current.must_change_password or current.token_version!=version:raise HTTPException(401,'Invalid or expired token')
    if admin and current.role!='admin':raise HTTPException(403,'Administrator access required')
    if not admin:creator(current)
    return current


def management_fingerprint():
    # ssh-keygen emits only the public fingerprint. The private file is neither
    # returned nor copied; the key continues to serve existing management SSH.
    path=settings.builder_ssh_key_path or settings.vpn_gateway_ssh_key_path
    try:
        result=subprocess.run(['ssh-keygen','-l','-f',path],capture_output=True,text=True,timeout=5)
        if result.returncode or len(result.stdout.split())<2:raise ValueError()
        return result.stdout.split()[1]
    except Exception:raise HTTPException(503,'Management-key safety verification is unavailable') from None


def key_manager():
    # Reuse source validation and host pinning, with bounded read-only AWS calls.
    import boto3
    from botocore.config import Config
    from app.ami_manager import AMIManager
    manager=AMIManager()
    if not manager.dry_run:
        manager._ec2_client=boto3.client('ec2',region_name=settings.aws_region,config=Config(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1}))
    return manager


def run_guest(builder,key):
    from app.vpn import VpnManager
    manager=key_manager();host=manager.validate_source_instance(builder.instance_id)
    groups=manager.ec2_client.describe_instances(InstanceIds=[builder.instance_id])['Reservations']
    instance=next(i for r in groups for i in r['Instances'] if i['InstanceId']==builder.instance_id)
    tags={t['Key']:t['Value'] for t in instance.get('Tags',[])}
    expected={'Project':'SecForge','ResourceType':'builder','BuilderId':f'bld-{builder.id}','RoomId':str(builder.room_id),'CreatorId':str(builder.creator_id)}
    if any(tags.get(k)!=v for k,v in expected.items()) or instance['State']['Name']!='running' or host!=builder.private_ip or instance.get('PrivateIpAddress')!=host:raise ValueError('Builder identity mismatch')
    console=manager.ec2_client.get_console_output(InstanceId=builder.instance_id,Latest=True).get('Output','')
    transport=VpnManager()
    body=program(builder.instance_id,builder.creator_id,key.revision,key.public_key,key.enabled,settings.builder_ssh_user,settings.builder_vpn_cidr)
    with transport.verified_builder_host(host,console):
        result=transport.run_builder_command(host,['sudo','-n','python3','-'],timeout=110,stdin_data=body)
    if 'SF_CREATOR_KEY_CONFIGURED' not in result.stdout.splitlines():raise ValueError('Guest did not confirm key configuration')


def apply_to_builder(db,builder,user,administrative=False,retire=False,finalize=True):
    fresh_user(db,user,admin=administrative)
    # Room lock and existing single-flight guard exclude capture/config races.
    if administrative:
        # Prevent a registry writer from holding the target user while waiting
        # on this key row, including FK checks during guest-ack persistence.
        if builder.creator_id != user.id:
            if db.scalar(select(User).where(User.id==builder.creator_id).with_for_update(read=True)) is None:raise HTTPException(404,'Account unavailable')
        from app.models import Room
        room=db.scalar(select(Room).where(Room.id==builder.room_id).with_for_update())
        if not room or room.creator_id!=builder.creator_id:raise HTTPException(404,'Builder unavailable')
    else:owned(builder.room_id,user,db)
    key=db.scalar(select(CreatorSshKey).where(CreatorSshKey.user_id==builder.creator_id).with_for_update().execution_options(populate_existing=True))
    if not key:raise HTTPException(409,'Register your personal public key first')
    if administrative and key.enabled:raise HTTPException(403,'Only explicit key revocation is permitted')
    db.refresh(builder)
    deadline=builder.expires_at.replace(tzinfo=timezone.utc) if builder.expires_at.tzinfo is None else builder.expires_at
    if not builder.instance_id or not builder.private_ip or key.enabled and not retire and (builder.status!='running' or deadline<=datetime.now(timezone.utc)):raise HTTPException(409,'A running, unexpired builder is required')
    if db.scalar(select(MachineTemplate.id).where(MachineTemplate.source_builder_environment_id==builder.id,MachineTemplate.status.in_(['creating','pending','unknown'])).limit(1)):raise HTTPException(409,'Wait for image capture to finish')
    try:
        with single_flight(builder_lock_key(builder.id)):
            from types import SimpleNamespace
            desired=SimpleNamespace(revision=key.revision,public_key=key.public_key,enabled=key.enabled and not retire)
            run_guest(builder,desired)
            grant=db.get(CreatorBuilderKey,builder.id)
            if grant is None:grant=CreatorBuilderKey(builder_id=builder.id,user_id=builder.creator_id)
            grant.revision=key.revision;grant.enabled=desired.enabled;grant.applied_at=datetime.utcnow();db.add(grant)
            if not retire:
                key.applied_builder_id=builder.id;key.applied_revision=key.revision;key.applied_at=datetime.utcnow()
            if finalize:key.pending=False
            from app.administration import audit
            audit(db,user.id,'builder',builder.id,'creator-key.apply','success',{'revision':key.revision,'enabled':desired.enabled})
            db.commit();return view(key,not administrative)
    except OperationInProgress:
        db.rollback();raise HTTPException(409,'Another builder operation is in progress') from None
    except HTTPException:raise
    except Exception:
        db.rollback();raise HTTPException(409,'Could not verify the builder key. Existing administrative access is preserved; retry when the builder is reachable.') from None


def gone_instances(builders):
    ids={b.instance_id for b in builders if b.instance_id}
    if not ids:return set()
    try:
        response=key_manager().ec2_client.describe_instances(Filters=[{'Name':'instance-id','Values':sorted(ids)}])
        if response.get('NextToken'):return set()
        states={i['InstanceId']:i['State']['Name'] for r in response['Reservations'] for i in r['Instances']}
        # Exact-ID filtering can safely return no rows for fully forgotten IDs.
        return {value for value in ids if states.get(value) in (None,'terminated')}
    except Exception:return set()


def sync_current(db,user,administrative=False,target_id=None,force=False):
    uid=target_id if administrative else user.id
    key=db.get(CreatorSshKey,uid)
    if not key:return view(None)
    revision=key.revision;enabled=key.enabled
    active=db.scalar(select(BuilderEnvironment).where(BuilderEnvironment.creator_id==uid,BuilderEnvironment.status.in_(['pending','provisioning','running','stopping'])).order_by(BuilderEnvironment.id.desc()).limit(1))
    candidates={active.id} if active else set()
    grants=list(db.scalars(select(CreatorBuilderKey.builder_id).where(CreatorBuilderKey.user_id==uid,CreatorBuilderKey.enabled.is_(True)).order_by(CreatorBuilderKey.builder_id).limit(33)))
    candidates.update(grants[:32])
    if not enabled and key.applied_builder_id:candidates.add(key.applied_builder_id)
    builders=list(db.scalars(select(BuilderEnvironment).where(BuilderEnvironment.id.in_(candidates),BuilderEnvironment.creator_id==uid).order_by(BuilderEnvironment.id))) if candidates else []
    gone=gone_instances([b for b in builders if b.status not in ('pending','provisioning','running','stopping')])
    pending=len(grants)>32;commands=0
    for builder in builders:
        retire=enabled and (active is None or builder.id!=active.id)
        desired=enabled and not retire
        grant=db.get(CreatorBuilderKey,builder.id)
        if builder.instance_id in gone:
            if grant:grant.enabled=False
            db.commit();continue
        if grant and grant.revision==revision and grant.enabled==desired and not (force and active and builder.id==active.id):continue
        # At most one bounded SSH operation per request. Additional historical
        # guests remain explicitly pending and are processed by a safe retry.
        if commands>=1:pending=True;continue
        commands+=1
        try:apply_to_builder(db,builder,user,administrative,retire=retire,finalize=False)
        except HTTPException:db.rollback();pending=True
        current=db.get(CreatorSshKey,uid)
        if current.revision!=revision:return view(current,not administrative)
    key=db.get(CreatorSshKey,uid)
    if key.revision==revision:key.pending=pending;db.commit()
    return view(key,not administrative)


@router.get('/users/me/creator-key',response_model=KeyResponse)
def my_key(user:User=Depends(require_role('creator')),db:Session=Depends(get_db)):
    return view(db.get(CreatorSshKey,user.id))


@router.put('/users/me/creator-key',response_model=KeyResponse)
def save_key(data:KeyWrite,user:User=Depends(require_role('creator')),db:Session=Depends(get_db)):
    fresh_user(db,user,exclusive=True)
    # Serialize first creation as well as replacement; no room/SSH lock is held
    # until this database-only mutation is durably committed.
    key=db.get(CreatorSshKey,user.id)
    if (key.revision if key else 0)!=data.revision:raise HTTPException(409,'Your key changed. Refresh before replacing it.')
    digest=fingerprint(data.public_key)
    if digest==management_fingerprint():raise HTTPException(422,'Register a personal key; the shared management key must not be distributed')
    if db.scalar(select(CreatorSshKey.user_id).where(CreatorSshKey.fingerprint==digest,CreatorSshKey.user_id!=user.id)):raise HTTPException(409,'Use a key dedicated to your own creator account')
    if key and not key.enabled and key.public_key==data.public_key:raise HTTPException(422,'Generate a new personal key after revocation')
    if key and key.enabled and key.public_key==data.public_key:
        db.commit();return sync_current(db,user)
    if not key:key=CreatorSshKey(user_id=user.id,public_key=data.public_key,fingerprint=digest,revision=1,enabled=True,pending=True)
    else:key.public_key=data.public_key;key.fingerprint=digest;key.enabled=True;key.revision+=1;key.pending=True
    key.updated_at=datetime.utcnow();db.add(key)
    from app.administration import audit
    audit(db,user.id,'creator-key',user.id,'creator-key.register','success',{'fingerprint':digest,'revision':key.revision})
    try:db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'Use a key dedicated to your own creator account') from None
    return sync_current(db,user)


def disable_key(db,target,user,revision,administrative=False,reason='owner-request',confirmed_username=None):
    if administrative:
        from app.administration import control,fresh_actor
        control(db);fresh_actor(db,user)
        target=db.scalar(select(User).where(User.id==target.id).with_for_update().execution_options(populate_existing=True))
        if target.username!=confirmed_username:raise HTTPException(409,'Account changed; confirm its current username')
    else:fresh_user(db,user,exclusive=True)
    key=db.get(CreatorSshKey,target.id)
    if not key:return view(None)
    if not key.enabled and revision in (key.revision,key.revision-1):
        db.commit();return sync_current(db,user,administrative,target.id if administrative else None)
    if key.revision!=revision:raise HTTPException(409,'The key changed. Refresh before revoking it.')
    key.enabled=False;key.revision+=1;key.pending=True;key.updated_at=datetime.utcnow()
    from app.administration import audit
    audit(db,user.id,'creator-key',target.id,'creator-key.revoke','success',{'reason':reason,'revision':key.revision,'guest_removal_pending':True})
    db.commit();return sync_current(db,user,administrative,target.id if administrative else None)


@router.delete('/users/me/creator-key',response_model=KeyResponse)
def remove_key(data:KeyRevision,user:User=Depends(require_role('creator')),db:Session=Depends(get_db)):
    return disable_key(db,user,user,data.revision)


@router.post('/rooms/{room_id}/builder/creator-key',response_model=KeyResponse)
def apply_key(room_id:int=Path(ge=1,le=2147483647),user:User=Depends(require_role('creator')),db:Session=Depends(get_db)):
    room=owned(room_id,user,db)
    if room.room_type!='PRACTICAL':raise HTTPException(409,'Theory rooms do not use SSH')
    builder=db.scalar(select(BuilderEnvironment).where(BuilderEnvironment.room_id==room_id,BuilderEnvironment.creator_id==user.id,BuilderEnvironment.status=='running').limit(1))
    if not builder:raise HTTPException(409,'A running builder is required')
    if db.scalar(select(MachineTemplate.id).where(MachineTemplate.source_builder_environment_id==builder.id,MachineTemplate.status.in_(['creating','pending','unknown'])).limit(1)):raise HTTPException(409,'Wait for image capture to finish')
    key=db.get(CreatorSshKey,user.id)
    if not key:raise HTTPException(409,'Register your personal public key first')
    key.pending=True;db.commit()
    return sync_current(db,user,force=True)


@router.get('/admin/users/{target_id}/creator-key',response_model=KeyResponse)
def admin_key(target_id:int=Path(ge=1,le=2147483647),user:User=Depends(require_role('admin')),db:Session=Depends(get_db)):
    if not db.get(User,target_id):raise HTTPException(404,'Account unavailable')
    return view(db.get(CreatorSshKey,target_id),False)


@router.post('/admin/users/{target_id}/creator-key/revoke',response_model=KeyResponse)
def revoke_key(data:AdminRevoke,target_id:int=Path(ge=1,le=2147483647),user:User=Depends(require_role('admin')),db:Session=Depends(get_db)):
    target=db.get(User,target_id)
    if not target or target.username!=data.confirm_username:raise HTTPException(409,'Confirm the exact account username')
    return disable_key(db,target,user,data.revision,True,data.reason,data.confirm_username)


def automatic_key(db,builder,user):
    if builder.status!='running':return
    key=db.get(CreatorSshKey,user.id)
    if not key or not key.enabled:return
    # Guest access can be retried from the UI; never alter lifecycle/cleanup
    # state or hide a running machine if boot/console/SSH is not ready yet.
    key.pending=True;db.commit()
    sync_current(db,user)
