"""Room policy and owned AWS synchronization, entirely offline."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import re

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, inspect, text
from pydantic import ValidationError

from app.cleanup import ownership
from app.lab_manager import LabManager, TerraformError
from app.models import BuilderEnvironment, LabInstance, Room, RoomDraft
from app.network_ports import DEFAULT_PORTS, RULE_DESCRIPTION, normalize_ports, room_ports, vpn_source
from app.network_sync import NetworkSyncError, sync_running_networks, sync_security_group
from app.network_ports_migration import migrate
from app.schemas import RoomCreate, RoomUpdate
from test_challenges import env, publish
from test_room_workspace import read, edit, release


PORTS = [{'protocol': 'tcp', 'from_port': 21, 'to_port': 21},
         {'protocol': 'tcp', 'from_port': 40000, 'to_port': 40100},
         {'protocol': 'udp', 'from_port': 53, 'to_port': 53}]


@pytest.mark.parametrize('value', [None, {}, '80', [{'protocol':'icmp','from_port':1,'to_port':1}],
    [{'protocol':'TCP','from_port':1,'to_port':1}], [{'protocol':'tcp','from_port':True,'to_port':1}],
    [{'protocol':'tcp','from_port':'80','to_port':80}], [{'protocol':'tcp','from_port':0,'to_port':80}],
    [{'protocol':'udp','from_port':65536,'to_port':65536}], [{'protocol':'tcp','from_port':90,'to_port':80}],
    [{'protocol':'tcp','from_port':1.1,'to_port':2}], [{'protocol':'tcp','from_port':1,'to_port':65535}],
    [{'protocol':'tcp','from_port':80,'to_port':80,'cidr':'0.0.0.0/0'}], PORTS * 7])
def test_invalid_configs(value):
    with pytest.raises((ValidationError, ValueError)):
        RoomUpdate(exposed_ports=value)


def test_normalizes_duplicates_overlaps_and_keeps_protocols_separate():
    assert normalize_ports(PORTS + PORTS + [{'protocol':'tcp','from_port':40050,'to_port':40200}]) == [
        PORTS[0], {'protocol':'tcp','from_port':40000,'to_port':40200}, PORTS[2]]
    assert normalize_ports([]) == []
    assert normalize_ports([{'protocol':'udp','from_port':65535,'to_port':65535}])[0]['to_port'] == 65535
    assert RoomCreate(name='Legacy',category='web-security',difficulty='easy').model_dump()['exposed_ports'] == DEFAULT_PORTS
    assert RoomUpdate(name='Only metadata').model_dump(exclude_unset=True) == {'name':'Only metadata'}


@pytest.mark.parametrize('cidr', ['0.0.0.0/0','::/0','8.8.8.0/24','10.0.0.0/8','127.0.0.0/24','169.254.0.0/16','10.8.0.1/24'])
def test_refuses_public_broad_or_invalid_vpn(cidr):
    with pytest.raises(ValueError): vpn_source(cidr)


@pytest.mark.parametrize('kind', ['builder','lab'])
def test_generated_tfvars_load_correct_room_policy(env, kind):
    publish(env)
    assert edit(env,'metadata',{'exposed_ports':PORTS}).status_code == 200
    with env.maker() as db:
        builder = BuilderEnvironment(id=700,room_id=env.room,creator_id=env.users['creator'],status='running',terraform_state_path='/tmp/offline-builder',expires_at=datetime.utcnow()+timedelta(hours=1))
        lab = LabInstance(id=701,room_id=env.room,learner_id=env.users['learner'],expires_at=datetime.utcnow()+timedelta(hours=1))
        test = LabInstance(id=702,room_id=env.room,learner_id=env.users['creator'],expires_at=datetime.utcnow()+timedelta(hours=1))
        db.add_all([builder,lab,test]);db.commit()
        manager=LabManager()
        for resource in ([builder] if kind=='builder' else [lab,test]):
            generated=manager._generate_tfvars(resource) if kind=='builder' else manager._generate_lab_tfvars(resource,'ami-offline')
            actual=json.loads(re.search(r'^exposed_ports\s*=\s*(.+)$',generated,re.M).group(1))
            assert actual == (DEFAULT_PORTS if resource is lab else PORTS)
    assert release(env).status_code == 200
    with env.maker() as db:
        assert room_ports(db,db.get(LabInstance,701),'lab') == PORTS


def test_old_draft_compatibility_and_empty_policy(env):
    assert read(env)['room']['exposed_ports'] == DEFAULT_PORTS
    assert edit(env,'metadata',{'name':'Old snapshot'}).status_code == 200
    with env.maker() as db:
        draft=db.get(RoomDraft,env.room)
        doc=json.loads(json.dumps(draft.document));doc['metadata'].pop('exposed_ports')
        draft.document=doc;db.commit()
        r=SimpleNamespace(room_id=env.room)
        assert room_ports(db,r,'builder') == DEFAULT_PORTS
    assert edit(env,'metadata',{'exposed_ports':[]}).status_code == 200
    assert read(env)['room']['exposed_ports'] == []


@pytest.mark.parametrize('role,status', [('learner',403),('other',404),('admin',404)])
def test_network_edits_require_ownership(env,role,status):
    assert env.client.patch(f'/api/rooms/{env.room}/workspace',headers=env.headers[role],json={
        'revision':0,'action':'metadata','data':{'exposed_ports':PORTS}}).status_code == status
    assert env.client.patch(f'/api/rooms/{env.room}',headers=env.headers[role],json={'exposed_ports':PORTS}).status_code == status
    assert read(env)['room']['exposed_ports'] == DEFAULT_PORTS


def test_stale_and_invalid_updates_are_atomic(env):
    assert edit(env,'metadata',{'exposed_ports':PORTS},0).status_code == 200
    assert edit(env,'metadata',{'exposed_ports':[]},0).status_code == 409
    assert edit(env,'metadata',{'exposed_ports':[{'protocol':'tcp','from_port':1,'to_port':65535}]}).status_code == 422
    assert read(env)['room']['exposed_ports'] == PORTS


def test_legacy_api_persists_ports_without_resetting_them_on_other_updates(env):
    with patch('app.network_sync.sync_room_background') as synchronize:
        response=env.client.patch(f'/api/rooms/{env.room}',headers=env.headers['creator'],json={'exposed_ports':PORTS})
        assert response.status_code==200 and response.json()['exposed_ports']==PORTS
        synchronize.assert_called_once_with(env.room)
    assert env.client.patch(f'/api/rooms/{env.room}',headers=env.headers['creator'],json={'name':'Renamed'}).json()['exposed_ports']==PORTS


def test_room_creation_accepts_protocols_and_ranges(env):
    response=env.client.post('/api/rooms',headers=env.headers['creator'],json={
        'name':'Ports test','category':'web-security','difficulty':'easy','exposed_ports':PORTS})
    assert response.status_code==201 and response.json()['exposed_ports']==PORTS


def test_invalid_stored_policy_fails_before_terraform_and_cleans_workspace(env,tmp_path):
    with env.maker() as db:
        db.get(Room,env.room).exposed_ports=[{'protocol':'all','from_port':0,'to_port':65535}]
        builder=BuilderEnvironment(room_id=env.room,creator_id=env.users['creator'],status='pending',
            terraform_state_path=str(tmp_path/'builder'),expires_at=datetime.utcnow()+timedelta(hours=1))
        db.add(builder);db.commit()
        manager=LabManager();manager.config['terraform_dry_run']=False
        with patch.object(manager,'_run_terraform') as terraform:
            with pytest.raises(TerraformError,match='Invalid room network policy'):manager.start_builder(builder)
            terraform.assert_not_called()
        assert builder.status=='failed' and not (tmp_path/'builder').exists()


def test_detached_persisted_environment_cannot_silently_use_default_ports(env):
    with env.maker() as db:
        lab=LabInstance(room_id=env.room,learner_id=env.users['learner'],expires_at=datetime.utcnow()+timedelta(hours=1))
        db.add(lab);db.commit();db.expunge(lab)
        with pytest.raises(ValueError,match='database session'):room_ports(None,lab,'lab')


@pytest.mark.parametrize('kind',['builder','lab'])
def test_cleanup_variable_reconstruction_does_not_depend_on_mutable_room_policy(env,kind):
    with env.maker() as db:
        db.get(Room,env.room).exposed_ports=[{'protocol':'invalid','from_port':0,'to_port':65535}]
        resource=(BuilderEnvironment(room_id=env.room,creator_id=env.users['creator'],status='stopping',
            terraform_state_path='/tmp/offline-builder',expires_at=datetime.utcnow()+timedelta(hours=1))
            if kind=='builder' else LabInstance(room_id=env.room,learner_id=env.users['learner'],
                status='stopping',expires_at=datetime.utcnow()+timedelta(hours=1)))
        db.add(resource);db.commit()
        manager=LabManager()
        values=manager._generate_tfvars(resource) if kind=='builder' else manager._generate_lab_tfvars(resource,'ami-offline')
        assert json.loads(re.search(r'^exposed_ports\s*=\s*(.+)$',values,re.M).group(1))==DEFAULT_PORTS


def aws_fixture(kind):
    resource=SimpleNamespace(id=1,room_id=10,creator_id=20,learner_id=30,instance_id='i-offline',security_group_id='sg-offline')
    tags=[{'Key':k,'Value':v} for k,v in ownership(resource,kind).items()]
    client=MagicMock()
    client.describe_security_groups.return_value={'SecurityGroups':[{'GroupId':resource.security_group_id,'VpcId':'vpc-test','Tags':tags}]}
    client.describe_instances.return_value={'Reservations':[{'Instances':[{'InstanceId':resource.instance_id,'VpcId':'vpc-test','Tags':tags,'State':{'Name':'running'},'SecurityGroups':[{'GroupId':resource.security_group_id}]}]}]}
    client.describe_network_interfaces.return_value={'NetworkInterfaces':[{'Attachment':{'InstanceId':resource.instance_id}}]}
    client.describe_security_group_rules.return_value={'SecurityGroupRules':[]}
    return client,resource


def rule(port, *, description=RULE_DESCRIPTION, protocol='tcp', cidr='10.9.0.0/24', **extra):
    return {'GroupId':'sg-offline','SecurityGroupRuleId':f'sgr-{port}-{protocol}','IsEgress':False,
            'IpProtocol':protocol,'FromPort':port,'ToPort':port,'CidrIpv4':cidr,'Description':description,**extra}


def sync(client,resource,kind='builder',ports=PORTS):
    sync_security_group(client,resource,kind,ports,'10.9.0.0/24','vpc-test')


@pytest.mark.parametrize('kind',['builder','lab'])
def test_builder_and_learner_rules_are_vpn_only_with_management(kind):
    client,resource=aws_fixture(kind);sync(client,resource,kind)
    permissions=[c.kwargs['IpPermissions'][0] for c in client.authorize_security_group_ingress.call_args_list]
    assert {(p['IpProtocol'],p['FromPort'],p['ToPort']) for p in permissions} == {('tcp',21,21),('tcp',22,22),('tcp',40000,40100),('udp',53,53)}
    assert all(p['IpRanges']==[{'CidrIp':'10.9.0.0/24','Description':RULE_DESCRIPTION}] for p in permissions)


def test_idempotent_existing_rules_and_removes_only_owned_service_rule():
    client,resource=aws_fixture('builder')
    client.describe_security_group_rules.return_value={'SecurityGroupRules':[
        rule(21,description='Manually allowed FTP'),rule(22,description='SSH management'),
        rule(80),rule(443,description='Unrelated'),rule(53,protocol='udp'),rule(9000,IsEgress=True)]}
    sync(client,resource,ports=[PORTS[0],PORTS[2]])
    client.authorize_security_group_ingress.assert_not_called()
    client.revoke_security_group_ingress.assert_called_once_with(GroupId='sg-offline',SecurityGroupRuleIds=['sgr-80-tcp'])


@pytest.mark.parametrize('mutation', ['group_tags','instance_tags','group_vpc','instance_vpc','shared','public','missing','unattached'])
def test_ambiguous_or_foreign_groups_never_mutated(mutation):
    client,resource=aws_fixture('builder')
    group=client.describe_security_groups.return_value['SecurityGroups'][0]
    instance=client.describe_instances.return_value['Reservations'][0]['Instances'][0]
    if mutation=='group_tags':group['Tags']=[]
    if mutation=='instance_tags':instance['Tags']=[]
    if mutation=='group_vpc':group['VpcId']='foreign'
    if mutation=='instance_vpc':instance['VpcId']='foreign'
    if mutation=='shared':client.describe_network_interfaces.return_value['NetworkInterfaces'].append({'Attachment':{'InstanceId':'i-foreign'}})
    if mutation=='public':instance['PublicIpAddress']='192.0.2.1'
    if mutation=='missing':client.describe_security_groups.return_value['SecurityGroups']=[]
    if mutation=='unattached':instance['SecurityGroups']=[]
    with pytest.raises(NetworkSyncError):sync(client,resource)
    client.authorize_security_group_ingress.assert_not_called();client.revoke_security_group_ingress.assert_not_called()


def error(code): return ClientError({'Error':{'Code':code,'Message':'sensitive AWS detail'}},'OfflineTest')


def test_duplicate_rule_safe_but_failed_add_never_removes_rules():
    client,resource=aws_fixture('builder')
    client.authorize_security_group_ingress.side_effect=error('InvalidPermission.Duplicate')
    sync(client,resource)
    client.authorize_security_group_ingress.side_effect=error('UnauthorizedOperation')
    client.describe_security_group_rules.return_value={'SecurityGroupRules':[rule(80)]}
    with pytest.raises(ClientError):sync(client,resource)
    client.revoke_security_group_ingress.assert_not_called()


def test_pagination_and_safe_missing_rule_retry():
    client,resource=aws_fixture('builder')
    client.describe_security_group_rules.side_effect=[{'SecurityGroupRules':[rule(22)],'NextToken':'next'}, {'SecurityGroupRules':[rule(80)]}]
    client.revoke_security_group_ingress.side_effect=error('InvalidSecurityGroupRuleId.NotFound')
    sync(client,resource,ports=[])
    assert client.describe_security_group_rules.call_args.kwargs['NextToken']=='next'
    client.authorize_security_group_ingress.assert_not_called()


def test_background_errors_leave_cleanup_metadata_and_retry(env, caplog):
    with env.maker() as db:
        builder=BuilderEnvironment(room_id=env.room,creator_id=env.users['creator'],status='running',terraform_state_path='/tmp/offline-builder',expires_at=datetime.utcnow()+timedelta(hours=1),instance_id='i-offline',security_group_id='sg-offline',error_message='existing cleanup metadata')
        db.add(builder);db.commit()
        with patch('app.network_sync.settings.terraform_dry_run',False),patch('app.network_sync.boto3.client',return_value=MagicMock()),patch('app.network_sync.sync_security_group',side_effect=error('UnauthorizedOperation')) as attempt:
            sync_running_networks(db,env.room);sync_running_networks(db,env.room)
            assert attempt.call_count==2
        assert builder.status=='running' and builder.error_message=='existing cleanup metadata'
        assert 'sensitive AWS detail' not in caplog.text


def test_dry_run_does_not_create_aws_clients(env):
    with env.maker() as db, patch('app.network_sync.settings.terraform_dry_run',True),patch('app.network_sync.boto3.client') as client:
        sync_running_networks(db);client.assert_not_called()


def test_network_update_skips_resources_locked_by_lifecycle(env):
    from app.ops_lock import single_flight, builder_lock_key
    with env.maker() as db:
        builder=BuilderEnvironment(room_id=env.room,creator_id=env.users['creator'],status='running',
            terraform_state_path='/tmp/offline-builder',expires_at=datetime.utcnow()+timedelta(hours=1),
            instance_id='i-offline',security_group_id='sg-offline')
        db.add(builder);db.commit()
        with single_flight(builder_lock_key(builder.id)), patch('app.network_sync.settings.terraform_dry_run',False),patch('app.network_sync.boto3.client') as client:
            sync_running_networks(db,env.room);client.assert_not_called()


def test_additive_migration_is_idempotent_preserves_rows_and_refuses_wrong_type():
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('CREATE TABLE rooms (id INTEGER PRIMARY KEY, name TEXT)'))
        c.execute(text("INSERT INTO rooms VALUES (1, 'Keep me')"))
        assert migrate(c) is True and migrate(c) is False
        row=c.exec_driver_sql('SELECT name, exposed_ports FROM rooms').one()
        assert row[0]=='Keep me' and json.loads(row[1])==DEFAULT_PORTS
        assert inspect(c).get_columns('rooms')[-1]['nullable'] is False
    engine.dispose()
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('CREATE TABLE rooms (id INTEGER PRIMARY KEY, exposed_ports TEXT NOT NULL)'))
        with pytest.raises(RuntimeError):migrate(c)
    engine.dispose()
