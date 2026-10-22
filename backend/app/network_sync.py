"""Reconcile only marked room rules on an exactly owned, private challenge SG.

Terraform owns SG/instance lifetimes. Ingress is initialized by Terraform and
subsequently reconciled here; unmarked legacy/manual/management rules stay intact.
"""
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.cleanup import ownership
from app.config import settings
from app.models import BuilderEnvironment, LabInstance
from app.network_ports import RULE_DESCRIPTION, room_ports, vpn_source
from app.ops_lock import OperationInProgress, builder_lock_key, lab_lock_key, single_flight

logger = logging.getLogger(__name__)


class NetworkSyncError(Exception):
    pass


def _tags(value, expected):
    tags = {t['Key']: t['Value'] for t in value.get('Tags', [])}
    return all(tags.get(k) == v for k, v in expected.items())


def sync_security_group(client, resource, kind, ports, cidr, vpc_id):
    """Fail closed before writes; add before removing; retry partial errors safely."""
    cidr = vpn_source(cidr)
    from app.network_ports import normalize_ports
    ports = normalize_ports(ports)
    expected = ownership(resource, kind)
    groups = client.describe_security_groups(GroupIds=[resource.security_group_id]).get('SecurityGroups', [])
    reservations = client.describe_instances(InstanceIds=[resource.instance_id]).get('Reservations', [])
    instances = [i for r in reservations for i in r.get('Instances', [])]
    interfaces = client.describe_network_interfaces(Filters=[{'Name': 'group-id', 'Values': [resource.security_group_id]}])
    if (len(groups) != 1 or len(instances) != 1 or interfaces.get('NextToken')
            or groups[0].get('GroupId') != resource.security_group_id
            or instances[0].get('InstanceId') != resource.instance_id
            or not _tags(groups[0], expected) or not _tags(instances[0], expected)
            or groups[0].get('VpcId') != vpc_id or instances[0].get('VpcId') != vpc_id
            or instances[0].get('PublicIpAddress')
            or instances[0].get('State', {}).get('Name') != 'running'
            or resource.security_group_id not in {g['GroupId'] for g in instances[0].get('SecurityGroups', [])}
            or not interfaces.get('NetworkInterfaces')
            or any(e.get('Attachment', {}).get('InstanceId') != resource.instance_id
                   or e.get('Association', {}).get('PublicIp') for e in interfaces['NetworkInterfaces'])):
        raise NetworkSyncError('Challenge network ownership or private attachment could not be verified')

    rules = []
    token = None
    for _ in range(10):
        args = {'Filters': [{'Name': 'group-id', 'Values': [resource.security_group_id]}], 'MaxResults': 1000}
        if token:
            args['NextToken'] = token
        page = client.describe_security_group_rules(**args)
        rules.extend(page.get('SecurityGroupRules', []))
        token = page.get('NextToken')
        if not token:
            break
    else:
        raise NetworkSyncError('Challenge rule inventory exceeded pagination limit')

    def key(r):
        return (r.get('IpProtocol'), r.get('FromPort'), r.get('ToPort'), r.get('CidrIpv4'))

    desired = {(r['protocol'], r['from_port'], r['to_port'], cidr) for r in ports}
    # Private VPN SSH remains infrastructure management access even if omitted.
    desired.add(('tcp', 22, 22, cidr))
    existing = {key(r) for r in rules if not r.get('IsEgress')}
    for protocol, start, end, source in sorted(desired - existing):
        try:
            client.authorize_security_group_ingress(GroupId=resource.security_group_id, IpPermissions=[{
                'IpProtocol': protocol, 'FromPort': start, 'ToPort': end,
                'IpRanges': [{'CidrIp': source, 'Description': RULE_DESCRIPTION}],
            }])
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') != 'InvalidPermission.Duplicate':
                raise
    # Do not adopt a pre-existing duplicate, nor remove manual/management rules.
    for rule in rules:
        if (rule.get('GroupId') == resource.security_group_id and not rule.get('IsEgress')
                and rule.get('Description') == RULE_DESCRIPTION
                and rule.get('IpProtocol') in ('tcp', 'udp') and key(rule) not in desired):
            try:
                client.revoke_security_group_ingress(GroupId=resource.security_group_id,
                    SecurityGroupRuleIds=[rule['SecurityGroupRuleId']])
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != 'InvalidSecurityGroupRuleId.NotFound':
                    raise


def sync_running_networks(db, room_id=None):
    if settings.terraform_dry_run:
        return
    client = None
    for kind, model in [('builder', BuilderEnvironment), ('lab', LabInstance)]:
        query = select(model.id).where(model.status == 'running')
        if room_id is not None:
            query = query.where(model.room_id == room_id)
        for identifier in db.scalars(query).all():
            try:
                lock = builder_lock_key(identifier) if kind == 'builder' else lab_lock_key(identifier)
                with single_flight(lock):
                    resource = db.scalar(select(model).where(model.id == identifier).execution_options(populate_existing=True))
                    if resource is None or resource.status != 'running':
                        continue
                    if not resource.security_group_id or not resource.instance_id:
                        raise NetworkSyncError('Running challenge has no recorded network identity')
                    ports = room_ports(db, resource, kind)
                    if client is None:
                        client = boto3.client('ec2', region_name=settings.aws_region,
                            config=Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 2}))
                    sync_security_group(client, resource, kind, ports, settings.builder_vpn_cidr, settings.builder_vpc_id)
            except OperationInProgress:
                continue
            except Exception:
                # AWS exception text may contain account/infrastructure details.
                # Keep runtime machine status and cleanup metadata unchanged.
                logger.warning('Room port synchronization failed for %s %s; will retry', kind, identifier)


def sync_room_background(room_id):
    if settings.terraform_dry_run:
        return
    from app.database import SessionLocal
    try:
        with SessionLocal() as db:
            sync_running_networks(db, room_id)
    except Exception:
        logger.warning('Room port synchronization unavailable; reconciliation will retry')
