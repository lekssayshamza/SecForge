"""Offline cleanup recovery and ownership checks; no real external operations."""
import asyncio
import copy
import json
import subprocess
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi import BackgroundTasks
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

from app.cleanup import CleanupInventory, CleanupVerificationError, ownership, state_resources
from app.lab_manager import LabManager, TerraformError
from app.models import Base, BuilderEnvironment, LabInstance, Room, User
from app.ops_lock import OperationInProgress
from app.reconciliation import ReconciliationWorker


PRESERVED_BUILDER_DIAGNOSTIC = (
    "Preserved for creator review: EC2 instance stopped outside SecForge; "
    "resources and Terraform state retained; cleanup not requested."
)


@pytest.fixture(params=['builder', 'lab'])
def env(request, tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path/'actors.sqlite'),
                           connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    creator = User(username='creator', email='creator@example.invalid', password_hash='offline', role='creator')
    learner = User(username='learner', email='learner@example.invalid', password_hash='offline', role='learner')
    db.add_all([creator, learner]); db.flush()
    room = Room(name='offline', creator_id=creator.id, category='Web', difficulty='easy', status='draft')
    db.add(room); db.flush()
    kind = request.param
    model = BuilderEnvironment if kind == 'builder' else LabInstance
    fields = {'creator_id': creator.id} if kind == 'builder' else {'learner_id': learner.id}
    resource = model(id=1, room_id=room.id, status='stopping', instance_id='i-offline',
                     security_group_id='sg-offline', private_ip='10.0.1.20',
                     terraform_state_path=str(tmp_path/kind/(kind+'-1')),
                     expires_at=datetime.utcnow()+timedelta(hours=1), **fields)
    db.add(resource); db.commit()
    manager = LabManager()
    manager.config.update(terraform_dry_run=False, terraform_workspace_root=str(tmp_path/'builder'),
                          lab_terraform_workspace_root=str(tmp_path/'lab'))
    tags = ownership(resource, kind)
    state = {'version': 4, 'resources': [
        {'mode': 'managed', 'type': 'aws_instance', 'instances': [{'attributes': {'id':'i-offline', 'tags':tags}}]},
        {'mode': 'managed', 'type': 'aws_security_group', 'instances': [{'attributes': {'id':'sg-offline', 'tags':tags}}]},
    ]}
    calls = []
    control = SimpleNamespace(fail=None, fail_once=False, destroyed=False, still_alive=False,
                              state=state, block=None, resume=None, no_state=False)
    def terraform(args, **kwargs):
        calls.append(args)
        if control.fail == args[0]:
            if control.fail_once: control.fail=None
            return subprocess.CompletedProcess(args,1,'','network connection failed; token=offline-sensitive-value')
        if args == ['state', 'pull'] and control.no_state:
            return subprocess.CompletedProcess(args, 1, '', 'No state file was found!')
        if args[0]=='destroy':
            if control.block:
                control.block.set()
                assert control.resume.wait(5), 'bounded actor timeout'
            control.destroyed=True
        output = json.dumps({'version':4,'resources':[]} if control.destroyed else control.state) if args==['state','pull'] else ''
        return subprocess.CompletedProcess(args,0,output,'')
    manager._run_terraform=terraform
    class Inventory:
        def remaining(self, expected, resource):
            if control.destroyed and not control.still_alive:
                return {'aws_instance':set(), 'aws_security_group':set()}
            return {'aws_instance':{'i-offline'},'aws_security_group':{'sg-offline'}}
    manager._cleanup_inventory=lambda:Inventory()
    monkeypatch.setattr('app.lab_manager.sync_vpn_authz',lambda db:None)
    stop=lambda: manager.stop_builder(resource) if kind=='builder' else manager.stop_lab_instance(resource,db)
    yield SimpleNamespace(db=db, engine=engine, model=model, resource=resource, creator=creator,
                          learner=learner, kind=kind, manager=manager, calls=calls, control=control, stop=stop)
    db.close();engine.dispose()


@pytest.mark.parametrize('phase',['init','destroy'])
def test_failure_preserves_stop_identifiers_workspace_and_retry(env, phase):
    env.control.fail=phase;env.control.fail_once=True
    with pytest.raises(TerraformError):env.stop()
    env.db.refresh(env.resource)
    assert env.resource.status=='stopping'
    assert env.resource.instance_id=='i-offline' and env.resource.security_group_id=='sg-offline'
    assert env.resource.stopped_at is None
    assert phase+' failed' in env.resource.error_message
    assert 'offline-sensitive-value' not in env.resource.error_message
    assert env.resource.terraform_state_path
    from pathlib import Path
    assert Path(env.resource.terraform_state_path).exists()
    if phase=='init': assert not any(c[0]=='destroy' for c in env.calls)
    env.stop()
    assert env.resource.status=='stopped' and env.resource.instance_id is None
    assert sum(c[0]=='init' for c in env.calls)==2
    assert not Path(env.resource.terraform_state_path).exists()
    before=len(env.calls);env.stop();assert len(env.calls)==before


def test_aws_still_running_after_destroy_is_not_reported_stopped(env):
    env.control.still_alive=True
    with pytest.raises(TerraformError,match='AWS resources still exist'):env.stop()
    assert env.resource.status=='stopping' and env.resource.instance_id=='i-offline'
    env.control.still_alive=False
    env.stop()
    assert env.resource.status=='stopped'
    assert sum(c[0]=='destroy' for c in env.calls)==1  # empty state: verify absence, no second destroy


@pytest.mark.parametrize('corruption',['foreign-tags','foreign-id','shared-resource','missing-state','missing-resource'])
def test_bad_or_stale_state_never_destroys(env,corruption):
    state=env.control.state
    if corruption=='foreign-tags':state['resources'][0]['instances'][0]['attributes']['tags']['RoomId']='999'
    elif corruption=='foreign-id':state['resources'][0]['instances'][0]['attributes']['id']='i-other'
    elif corruption=='shared-resource':state['resources'].append({'mode':'managed','type':'aws_vpc','instances':[]})
    elif corruption=='missing-state':env.control.fail='state'
    elif corruption=='missing-resource':state['resources']=[]
    with pytest.raises(TerraformError):env.stop()
    assert not any(c[0]=='destroy' for c in env.calls)
    assert env.resource.status=='stopping' and env.resource.instance_id=='i-offline'


def test_wrong_workspace_backend_never_runs_terraform(env):
    from pathlib import Path
    p=Path(env.resource.terraform_state_path);p.mkdir(parents=True)
    (p/'backend.tf').write_text('terraform { backend "s3" {} }')
    with pytest.raises(TerraformError,match='remote-state target mismatch'):env.stop()
    assert not env.calls


def test_retry_preserves_existing_configuration(env):
    from pathlib import Path
    workspace = Path(env.resource.terraform_state_path)
    workspace.mkdir(parents=True)
    backend = (env.manager._generate_backend_config(env.resource) if env.kind == 'builder'
               else env.manager._generate_lab_backend_config(env.resource))
    (workspace / 'backend.tf').write_text(backend)
    (workspace / 'terraform.tfvars').write_text('ami_id = "ami-selected-offline"\n')
    env.control.fail = 'init'
    with pytest.raises(TerraformError):
        env.stop()
    assert (workspace / 'terraform.tfvars').read_text() == 'ami_id = "ami-selected-offline"\n'


def test_workspace_outside_owned_root_is_preserved(env, tmp_path):
    from pathlib import Path
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    sentinel = unrelated / 'preserve.txt'
    sentinel.write_text('unrelated work')
    env.resource.terraform_state_path = str(unrelated)
    env.db.commit()
    with pytest.raises(TerraformError, match='outside configured environment root'):
        env.stop()
    assert sentinel.read_text() == 'unrelated work' and not env.calls


def test_absent_state_with_running_resource_never_destroys(env):
    env.control.no_state = True
    with pytest.raises(TerraformError, match='missing from Terraform state'):
        env.stop()
    assert env.resource.status == 'stopping' and env.resource.instance_id == 'i-offline'
    assert not any(c[0] == 'destroy' for c in env.calls)


def test_stop_before_apply_with_no_state_verifies_absence(env):
    env.resource.instance_id = env.resource.security_group_id = env.resource.private_ip = None
    env.resource.status = 'pending'
    env.db.commit()
    env.control.no_state = env.control.destroyed = True
    env.stop()
    assert env.resource.status == 'stopped'
    assert not any(c[0] == 'destroy' for c in env.calls)


def test_completion_database_failure_retains_workspace_and_retries_verification(env, monkeypatch):
    from pathlib import Path
    from sqlalchemy.exc import SQLAlchemyError
    original = env.db.commit
    calls = 0
    def commit():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SQLAlchemyError('offline completion failure')
        original()
    monkeypatch.setattr(env.db, 'commit', commit)
    with pytest.raises(TerraformError, match='database completion failed'):
        env.stop()
    env.db.refresh(env.resource)
    assert env.resource.status == 'stopping' and env.resource.instance_id == 'i-offline'
    assert Path(env.resource.terraform_state_path).exists()
    env.stop()
    assert env.resource.status == 'stopped'
    assert sum(c[0] == 'destroy' for c in env.calls) == 1


@pytest.mark.parametrize('state', [{}, {'version': 4, 'resources': [None]},
                                 {'version': 4, 'resources': [{'mode': 'managed', 'type': 'aws_instance'}]}])
def test_malformed_state_is_not_treated_as_empty(env, state):
    env.control.state = state
    with pytest.raises(TerraformError):
        env.stop()
    assert env.resource.status == 'stopping'
    assert not any(c[0] == 'destroy' for c in env.calls)


def test_repeated_stop_api_returns_current_state_without_new_task(env):
    from app.labs import stop_lab
    from app.builder import stop_builder
    for state in ['stopping','stopped']:
        env.resource.status=state;env.db.commit()
        tasks=BackgroundTasks()
        result=(stop_builder(env.resource.room_id,env.creator,env.db) if env.kind=='builder'
                else stop_lab(env.resource.room_id,tasks,env.learner,env.db))
        assert result.status==state and not tasks.tasks
        assert not env.calls


@pytest.mark.parametrize('history_status', ['stopped', 'failed'])
@pytest.mark.parametrize('current_status', ['stopping', 'stopped'])
def test_stop_and_status_select_current_environment_over_history(env, history_status, current_status):
    from app.builder import get_builder_or_404, stop_builder
    from app.labs import get_learner_lab_or_404, stop_lab
    # Production's partial unique indexes permit terminal history. SQLite's
    # metadata-only indexes are unconditional, so remove only this test index.
    index = ('ix_one_active_builder_per_creator' if env.kind == 'builder'
             else 'ix_one_active_lab_per_learner_room')
    env.db.execute(text('DROP INDEX ' + index))
    fields = {'creator_id': env.creator.id} if env.kind == 'builder' else {'learner_id': env.learner.id}
    history = env.model(id=0, room_id=env.resource.room_id, status=history_status,
                        terraform_state_path='unused-offline-history',
                        expires_at=datetime.utcnow(), created_at=datetime.utcnow() - timedelta(days=1), **fields)
    env.resource.status = current_status
    env.db.add(history)
    env.db.commit()
    tasks = BackgroundTasks()
    if env.kind == 'builder':
        status = get_builder_or_404(env.resource.room_id, env.creator, env.db)
        result = stop_builder(env.resource.room_id, env.creator, env.db)
    else:
        status = get_learner_lab_or_404(env.resource.room_id, env.learner.id, env.db)
        result = stop_lab(env.resource.room_id, tasks, env.learner, env.db)
    assert status.id == result.id == env.resource.id
    assert result.status == current_status and history.status == history_status
    assert not tasks.tasks and not env.calls


@pytest.mark.parametrize('winner_status', ['stopping', 'stopped', 'failed'])
def test_stop_losing_conditional_claim_never_schedules_cleanup(env, monkeypatch, winner_status):
    from fastapi import HTTPException
    from app.builder import stop_builder
    from app.labs import stop_lab
    env.resource.status = 'running'
    env.db.commit()
    resource_id = env.resource.id
    execute = env.db.execute
    def lose_claim(statement, *args, **kwargs):
        if statement.is_update:
            with Session(env.engine) as other:
                other.execute(update(env.model).where(env.model.id == resource_id).values(status=winner_status))
                other.commit()
        return execute(statement, *args, **kwargs)
    monkeypatch.setattr(env.db, 'execute', lose_claim)
    tasks = BackgroundTasks()
    stop = (lambda: stop_builder(env.resource.room_id, env.creator, env.db)) if env.kind == 'builder' else (
        lambda: stop_lab(env.resource.room_id, tasks, env.learner, env.db))
    if winner_status == 'failed':
        with pytest.raises(HTTPException) as exc:
            stop()
        assert exc.value.status_code == 409
    else:
        assert stop().status == winner_status
    assert not tasks.tasks and not env.calls


def test_legacy_cleanup_failure_is_recovered_by_existing_reconciliation(env,monkeypatch):
    env.resource.status='failed'
    env.resource.error_message='Failed to reinitialize Terraform workspace for destroy'
    env.resource.updated_at=datetime.utcnow()-timedelta(hours=1)
    env.db.commit()
    monkeypatch.setattr('app.reconciliation.LabManager',lambda:env.manager)
    worker=ReconciliationWorker()
    handler=worker._handle_crashed_stopping_builders if env.kind=='builder' else worker._handle_crashed_stopping_labs
    asyncio.run(handler(env.db))
    env.db.refresh(env.resource)
    assert env.resource.status=='stopped'


def test_reconciliation_failure_stays_eligible_but_respects_backoff(env,monkeypatch):
    env.resource.updated_at=datetime.utcnow()-timedelta(hours=1);env.db.commit()
    env.control.fail='init'
    monkeypatch.setattr('app.reconciliation.LabManager',lambda:env.manager)
    worker=ReconciliationWorker()
    handler=worker._handle_crashed_stopping_builders if env.kind=='builder' else worker._handle_crashed_stopping_labs
    asyncio.run(handler(env.db));env.db.refresh(env.resource)
    assert env.resource.status=='stopping' and env.resource.error_message.startswith('Cleanup retry required:')
    count=len(env.calls);asyncio.run(handler(env.db));assert len(env.calls)==count
    env.control.fail=None
    env.resource.updated_at=datetime.utcnow()-timedelta(hours=1);env.db.commit()
    asyncio.run(handler(env.db));assert env.resource.status=='stopped'


def test_concurrent_destroy_is_deferred_and_runs_exactly_once(env):
    env.control.block=threading.Event();env.control.resume=threading.Event()
    errors=[]
    def actor():
        with Session(env.engine) as db:
            row=db.get(env.model,env.resource.id)
            try:
                if env.kind=='builder':env.manager.stop_builder(row)
                else:env.manager.stop_lab_instance(row,db)
            except BaseException as e:errors.append(e)
    thread=threading.Thread(target=actor);thread.start()
    assert env.control.block.wait(5)
    try:
        with pytest.raises(OperationInProgress):env.stop()
        assert not env.control.destroyed
    finally:
        env.control.resume.set();thread.join(5)
    assert not thread.is_alive() and not errors
    env.db.refresh(env.resource)
    assert env.resource.status=='stopped' and sum(c[0]=='destroy' for c in env.calls)==1


def test_newer_terminal_transition_is_not_overwritten(env):
    original=env.manager._destroy_environment
    def lose_claim(resource,kind):
        workspace=original(resource,kind)
        with Session(env.engine) as other:
            other.execute(update(env.model).where(env.model.id==resource.id).values(status='failed',error_message='newer actor'))
            other.commit()
        return workspace
    env.manager._destroy_environment=lose_claim
    env.stop();env.db.refresh(env.resource)
    assert env.resource.status=='failed' and env.resource.error_message=='newer actor'
    assert env.resource.instance_id=='i-offline'
    from pathlib import Path
    assert Path(env.resource.terraform_state_path).exists()


@pytest.mark.parametrize('problem',['changed-tags','unrelated-attachment','access-denied'])
def test_aws_inventory_refuses_uncertain_or_unrelated_resources(problem):
    resource=SimpleNamespace(id=1,room_id=1,creator_id=1,instance_id='i-offline',security_group_id='sg-offline')
    expected=ownership(resource,'builder')
    tag_list=[{'Key':k,'Value':v} for k,v in expected.items()]
    class Client:
        def describe_instances(self,**kw):
            if problem=='access-denied':raise ClientError({'Error':{'Code':'AccessDenied'}},'DescribeInstances')
            tags=copy.deepcopy(tag_list)
            if problem=='changed-tags' and 'InstanceIds' in kw:tags[0]['Value']='OtherProject'
            return {'Reservations':[{'Instances':[{'InstanceId':'i-offline','State':{'Name':'running'},'Tags':tags}]}]}
        def describe_security_groups(self,**kw):return {'SecurityGroups':[{'GroupId':'sg-offline','Tags':tag_list}]}
        def describe_network_interfaces(self,**kw):return {'NetworkInterfaces':[{'Attachment':{'InstanceId':'i-other' if problem=='unrelated-attachment' else 'i-offline'}}]}
    inventory=object.__new__(CleanupInventory);inventory.client=Client()
    with pytest.raises((CleanupVerificationError,ClientError)):inventory.remaining(expected,resource)


@pytest.mark.parametrize('env', ['builder'], indirect=True)
def test_preserved_failed_builder_survives_reconciliation_and_orphan_discovery(env, monkeypatch):
    """An expired lease is not a cleanup request for a preserved failed builder."""
    from pathlib import Path
    from sqlalchemy.orm import sessionmaker
    from app.cleanup import cleanup_failed

    env.resource.status = 'failed'
    env.resource.error_message = PRESERVED_BUILDER_DIAGNOSTIC
    env.resource.expires_at = datetime.utcnow() - timedelta(days=1)
    env.resource.updated_at = datetime.utcnow() - timedelta(days=1)
    env.db.commit()
    workspace = Path(env.resource.terraform_state_path)
    workspace.mkdir(parents=True)
    sentinel = workspace / 'retained-state.txt'
    sentinel.write_text('creator-owned state')
    before = {name: getattr(env.resource, name) for name in (
        'status', 'error_message', 'instance_id', 'security_group_id',
        'private_ip', 'terraform_state_path', 'expires_at', 'updated_at', 'stopped_at',
    )}
    assert not cleanup_failed(env.resource)

    def unexpected_destroy():
        pytest.fail('Preserved builder must never reach Terraform cleanup')

    monkeypatch.setattr('app.reconciliation.SessionLocal', sessionmaker(bind=env.engine))
    monkeypatch.setattr('app.reconciliation.LabManager', unexpected_destroy)
    monkeypatch.setattr('app.reconciliation.sync_vpn_authz', lambda db: None)
    monkeypatch.setattr('app.reconciliation.sync_backend_routes', lambda db: None)
    worker = ReconciliationWorker()
    worker.config['terraform_dry_run'] = False
    monkeypatch.setattr(worker._discovery_service, 'discover_all_builder_resources', lambda: [
        SimpleNamespace(builder_id='bld-1', instances=[], security_groups=[]),
    ])
    # Repeated complete passes, including the read-only orphan path, must retain
    # the record even after every cleanup grace period has expired.
    for _ in range(2):
        asyncio.run(worker._reconcile_once())
        asyncio.run(worker._discover_orphans())
    env.db.refresh(env.resource)
    assert {name: getattr(env.resource, name) for name in before} == before
    assert sentinel.read_text() == 'creator-owned state'
    assert not env.calls and not env.control.destroyed
    assert worker._claim_builder_for_stop(env.db, env.resource.id) is None


@pytest.mark.parametrize('env', ['builder'], indirect=True)
def test_preserved_failed_builder_stop_rejects_without_destroy(env):
    from app.builder import stop_builder
    from fastapi import HTTPException

    env.resource.status = 'failed'
    env.resource.error_message = PRESERVED_BUILDER_DIAGNOSTIC
    env.db.commit()
    with pytest.raises(HTTPException) as error:
        stop_builder(env.resource.room_id, env.creator, env.db)
    assert error.value.status_code == 409
    env.db.refresh(env.resource)
    assert env.resource.status == 'failed'
    assert env.resource.error_message == PRESERVED_BUILDER_DIAGNOSTIC
    assert env.resource.instance_id == 'i-offline'
    assert env.resource.security_group_id == 'sg-offline'
    assert env.resource.stopped_at is None
    assert not env.calls


@pytest.mark.parametrize('env', ['builder'], indirect=True)
def test_preserved_failed_builder_has_no_vpn_or_management_route(env, monkeypatch):
    from app.config import settings
    from app.models import VpnIdentity
    from app.backend_routes import desired_builder_ips
    from app.vpn_authz import desired_pairs

    system = User(username='backend', email='backend@example.invalid',
                  password_hash='offline', role='system')
    env.db.add(system)
    env.db.flush()
    env.db.add_all([
        VpnIdentity(user_id=env.creator.id, common_name=f'sf-{env.creator.id}', vpn_ip='10.8.0.70'),
        VpnIdentity(user_id=system.id, common_name='sf-backend', vpn_ip='10.8.0.71'),
    ])
    monkeypatch.setattr(settings, 'vpn_enabled', True)
    monkeypatch.setattr(settings, 'terraform_dry_run', False)
    monkeypatch.setattr(settings, 'vpn_backend_authz_enabled', True)
    env.resource.status = 'running'
    env.db.commit()
    assert desired_pairs(env.db) == {
        ('10.8.0.70', env.resource.private_ip), ('10.8.0.71', env.resource.private_ip),
    }
    assert desired_builder_ips(env.db) == {env.resource.private_ip}
    env.resource.status = 'failed'
    env.resource.error_message = PRESERVED_BUILDER_DIAGNOSTIC
    env.db.commit()
    assert desired_pairs(env.db) == set()
    assert desired_builder_ips(env.db) == set()
    assert env.resource.instance_id == 'i-offline' and env.resource.stopped_at is None


@pytest.mark.parametrize('env', ['builder'], indirect=True)
def test_expired_disposable_cleanup_continues_beside_preserved_builder(env, monkeypatch, tmp_path):
    from sqlalchemy.orm import sessionmaker

    creator = User(username='historical', email='historical@example.invalid',
                   password_hash='offline', role='creator')
    env.db.add(creator)
    env.db.flush()
    room = Room(name='preserved', creator_id=creator.id, category='Web',
                difficulty='easy', status='draft')
    env.db.add(room)
    env.db.flush()
    workspace = tmp_path / 'preserved-builder'
    workspace.mkdir()
    sentinel = workspace / 'state.txt'
    sentinel.write_text('retained creator state')
    preserved = BuilderEnvironment(
        id=2, room_id=room.id, creator_id=creator.id, status='failed',
        instance_id='i-preserved', security_group_id='sg-preserved', private_ip='10.0.1.99',
        terraform_state_path=str(workspace), error_message=PRESERVED_BUILDER_DIAGNOSTIC,
        expires_at=datetime.utcnow() - timedelta(days=1),
        updated_at=datetime.utcnow() - timedelta(days=1),
    )
    env.db.add(preserved)
    env.resource.status = 'running'
    env.resource.expires_at = datetime.utcnow() - timedelta(hours=1)
    env.db.commit()
    monkeypatch.setattr('app.reconciliation.SessionLocal', sessionmaker(bind=env.engine))
    monkeypatch.setattr('app.reconciliation.LabManager', lambda: env.manager)
    monkeypatch.setattr('app.reconciliation.sync_vpn_authz', lambda db: None)
    monkeypatch.setattr('app.reconciliation.sync_backend_routes', lambda db: None)

    asyncio.run(ReconciliationWorker()._reconcile_once())
    env.db.refresh(env.resource)
    env.db.refresh(preserved)
    assert env.resource.status == 'stopped'
    assert sum(call[0] == 'destroy' for call in env.calls) == 1
    assert preserved.status == 'failed' and preserved.error_message == PRESERVED_BUILDER_DIAGNOSTIC
    assert preserved.instance_id == 'i-preserved' and preserved.security_group_id == 'sg-preserved'
    assert preserved.stopped_at is None and preserved.terraform_state_path == str(workspace)
    assert sentinel.read_text() == 'retained creator state'
