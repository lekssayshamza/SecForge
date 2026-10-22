"""Guest authentication changes only on an explicitly owned, verified builder."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.database import get_db
from app.config import settings
from app.models import BuilderEnvironment, MachineTemplate, User
from app.schemas import BuilderResponse
from app.guest_ssh import SshAuthWrite, script
from app.challenges import ConfidentialValidationRoute
from app.ops_lock import single_flight, builder_lock_key, OperationInProgress
from app.room_workspace import owned, private_response

router = APIRouter(prefix='/rooms', tags=['Builder SSH'], route_class=ConfidentialValidationRoute, dependencies=[Depends(private_response)])


def configure(builder, data):
    from app.ami_manager import AMIManager
    from app.vpn import VpnManager
    manager = AMIManager()
    host = manager.validate_source_instance(builder.instance_id)
    instances = manager.ec2_client.describe_instances(InstanceIds=[builder.instance_id])['Reservations'][0]['Instances']
    instance = next(i for i in instances if i['InstanceId'] == builder.instance_id)
    tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
    expected = {'Project': 'SecForge', 'ResourceType': 'builder', 'BuilderId': f'bld-{builder.id}', 'RoomId': str(builder.room_id), 'CreatorId': str(builder.creator_id)}
    if any(tags.get(k) != v for k, v in expected.items()) or instance['State']['Name'] != 'running' or host != builder.private_ip or instance.get('PrivateIpAddress') != host:
        raise ValueError('Builder identity mismatch')
    remote = VpnManager()
    console = manager.ec2_client.get_console_output(InstanceId=builder.instance_id, Latest=True).get('Output', '')
    program = script(data, settings.builder_vpn_cidr, settings.builder_ssh_user)
    with remote.verified_builder_host(host, console):
        remote.run_builder_command(host, ['sudo', '-n', 'python3', '-'], timeout=100, stdin_data=program)


@router.post('/{room_id}/builder/ssh-auth', response_model=BuilderResponse)
def set_auth(room_id: int, data: SshAuthWrite, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    room = owned(room_id, user, db)
    if room.room_type != 'PRACTICAL': raise HTTPException(409, 'Theory rooms do not use SSH')
    if data.mode == 'password' and data.password is None: raise HTTPException(422, 'Set an explicit training account password')
    builder = db.scalar(select(BuilderEnvironment).where(BuilderEnvironment.room_id == room_id, BuilderEnvironment.creator_id == user.id, BuilderEnvironment.status == 'running').order_by(BuilderEnvironment.id.desc()).limit(1))
    if builder is None or not builder.instance_id or not builder.private_ip: raise HTTPException(409, 'A running builder is required')
    deadline = builder.expires_at.replace(tzinfo=timezone.utc) if builder.expires_at.tzinfo is None else builder.expires_at
    if deadline <= datetime.now(timezone.utc): raise HTTPException(409, 'Builder lease has expired')
    if db.scalar(select(MachineTemplate.id).where(MachineTemplate.source_builder_environment_id == builder.id, MachineTemplate.status.in_(['pending', 'creating', 'unknown'])).limit(1)):
        raise HTTPException(409, 'Wait for image capture to finish before changing guest SSH settings')
    try:
        with single_flight(builder_lock_key(builder.id)):
            # Keep the room lock until the bounded guest operation completes so
            # capture cannot register or publish during guest configuration.
            configure(builder, data)
            builder.ssh_auth_mode = data.mode
            builder.ssh_guest_username = data.username
            db.commit()
            db.refresh(builder)
    except OperationInProgress:
        db.rollback()
        raise HTTPException(409, 'Another builder operation is in progress') from None
    except Exception:
        db.rollback()
        raise HTTPException(409, 'Could not verify guest SSH settings. Check that the training account exists, is nonprivileged, and the builder is reachable. Management key access is preserved.') from None
    return builder
