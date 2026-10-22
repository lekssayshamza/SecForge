"""Creator public-key isolation; synthetic keys, no SSH/AWS or production data."""
from unittest.mock import patch
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519,rsa
from sqlalchemy import select,func,text
from test_challenges import env as base_env
from test_builder_ssh_auth import builder
from app.models import CreatorSshKey,AdminAudit,AdminControl,BuilderEnvironment,User
from app.creator_keys import normalize_key,fingerprint
from app.creator_keys_migration import migrate

@pytest.fixture
def env(base_env):
    # Historical key-management handlers remain available only to this isolated
    # archival test application. The real production app does not mount them.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.creator_keys import router
    legacy=FastAPI();legacy.include_router(router,prefix='/api')
    original=base_env.client
    legacy.dependency_overrides.update(original.app.dependency_overrides)
    with TestClient(legacy) as client:
        base_env.client=client
        yield base_env
    base_env.client=original

@pytest.fixture(autouse=True)
def no_management_secret(monkeypatch):monkeypatch.setattr('app.creator_keys.management_fingerprint',lambda:'SHA256:isolated-management-placeholder')

def key():return ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()

def save(e,public=None,revision=0,role='creator'):
    return e.client.put('/api/users/me/creator-key',headers=e.headers[role],json={'public_key':public or key(),'revision':revision})

def test_registry_contains_only_public_key_and_private_owner_response(env):
    pub=key();response=save(env,pub);assert response.status_code==200,response.text
    data=response.json();assert data['enabled'] and data['revision']==1 and data['public_key']==pub
    assert data['fingerprint']==fingerprint(pub) and not data['pending']
    assert 'private, no-store' in response.headers['cache-control']
    assert env.client.get('/api/users/me/creator-key').status_code==401
    assert env.client.get('/api/users/me/creator-key',headers=env.headers['learner']).status_code==403
    assert save(env,role='learner').status_code==403
    assert env.client.get('/api/users/me/creator-key',headers=env.headers['other']).json()['configured'] is False
    assert env.client.get(f'/api/admin/users/{env.users["creator"]}/creator-key',headers=env.headers['creator']).status_code==403
    admin=env.client.get(f'/api/admin/users/{env.users["creator"]}/creator-key',headers=env.headers['admin']).json()
    assert admin['public_key'] is None and admin['fingerprint']==fingerprint(pub)
    with env.maker() as db:
        assert db.scalar(select(func.count()).select_from(CreatorSshKey))==1
        assert db.scalar(select(AdminAudit.action))=='creator-key.register'
        assert pub not in str(db.scalar(select(AdminAudit.context)))

@pytest.mark.parametrize('value',['-----BEGIN PRIVATE KEY-----\nsecret','ssh-ed25519 invalid','command="id" ssh-ed25519 AAAA','ssh-dss AAAA','https://external.invalid','ssh-rsa AAAA\nsecond-line'])
def test_private_invalid_and_option_keys_rejected_without_echo(env,value):
    response=save(env,value);assert response.status_code==422
    assert value not in response.text
    with env.maker() as db:assert db.scalar(select(func.count()).select_from(CreatorSshKey))==0


def test_algorithms_normalization_minimum_rsa_size_and_management_key_rejection(env):
    pub=key();assert normalize_key(pub+' harmless-comment')==pub
    weak=rsa.generate_private_key(public_exponent=65537,key_size=2048).public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    assert save(env,weak).status_code==422
    strong=rsa.generate_private_key(public_exponent=65537,key_size=3072).public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    assert normalize_key(strong)==strong
    with patch('app.creator_keys.management_fingerprint',return_value=fingerprint(pub)):assert save(env,pub).status_code==422


def test_revision_and_cross_account_key_uniqueness(env):
    pub=key();assert save(env,pub).status_code==200
    assert save(env,key(),0).status_code==409
    assert save(env,pub,role='other').status_code==409
    same=save(env,pub,1);assert same.status_code==200 and same.json()['revision']==1
    replacement=save(env,key(),1);assert replacement.status_code==200 and replacement.json()['revision']==2
    stale=env.client.request('DELETE','/api/users/me/creator-key',headers=env.headers['creator'],json={'revision':1});assert stale.status_code==409
    removed=env.client.request('DELETE','/api/users/me/creator-key',headers=env.headers['creator'],json={'revision':2});assert removed.status_code==200 and not removed.json()['enabled']
    assert env.client.request('DELETE','/api/users/me/creator-key',headers=env.headers['creator'],json={'revision':2}).status_code==200


def test_apply_only_running_owned_practical_builder_and_no_lifecycle_change(env):
    bid=builder(env);pub=key()
    with patch('app.creator_keys.run_guest') as guest:
        result=save(env,pub);assert result.status_code==200,result.text
        assert guest.call_count==1 and result.json()['applied_builder_id']==bid and not result.json()['pending']
        for role,status in [('other',404),('learner',403),('admin',404)]:
            assert env.client.post(f'/api/rooms/{env.room}/builder/creator-key',headers=env.headers[role]).status_code==status
        assert guest.call_count==1
        assert env.client.post(f'/api/rooms/{env.room}/builder/creator-key',headers=env.headers['creator']).status_code==200
    with env.maker() as db:assert db.get(BuilderEnvironment,bid).status=='running'


def test_failed_key_install_and_revocation_remain_explicitly_pending(env):
    bid=builder(env)
    with patch('app.creator_keys.run_guest',side_effect=RuntimeError('synthetic-not-for-response')):
        result=save(env);assert result.status_code==200 and result.json()['pending'] and result.json()['applied_at'] is None
        assert 'synthetic' not in result.text
        response=env.client.request('DELETE','/api/users/me/creator-key',headers=env.headers['creator'],json={'revision':1})
        assert response.status_code==200 and response.json()['pending'] and not response.json()['enabled']
    with env.maker() as db:assert db.get(BuilderEnvironment,bid).status=='running'


def test_admin_revocation_confirmation_authority_and_audit(env):
    save(env)
    with env.maker() as db:db.add(AdminControl(id=1));db.commit()
    url=f'/api/admin/users/{env.users["creator"]}/creator-key/revoke'
    data={'revision':1,'confirm_username':'creator','reason':'owner-request'}
    assert env.client.post(url,headers=env.headers['other'],json=data).status_code==403
    assert env.client.post(url,headers=env.headers['admin'],json={**data,'confirm_username':'wrong'}).status_code==409
    response=env.client.post(url,headers=env.headers['admin'],json=data)
    assert response.status_code==200 and not response.json()['enabled'] and response.json()['public_key'] is None
    with env.maker() as db:
        events=list(db.scalars(select(AdminAudit).where(AdminAudit.action=='creator-key.revoke')))
        assert len(events)==1 and events[0].actor_id==env.users['admin']
        assert db.get(User,env.users['creator']).role=='creator'


def test_additive_registry_migration_is_idempotent_and_preserves_accounts(env):
    with env.maker() as db:engine=db.get_bind()
    with engine.begin() as c:
        before=c.execute(text('SELECT id,username,role,password_hash FROM users ORDER BY id')).all()
        CreatorSshKey.__table__.drop(c)
        assert migrate(c) and not migrate(c)
        assert c.execute(text('SELECT id,username,role,password_hash FROM users ORDER BY id')).all()==before


def test_theory_and_capture_conflicts_do_not_install_keys(env):
    from app.models import Room,RoomTemplate,MachineTemplate
    bid=builder(env)
    with patch('app.creator_keys.run_guest') as guest:
        save(env)
        with env.maker() as db:
            t=RoomTemplate(room_id=env.room,version=1,created_by=env.users['creator']);db.add(t);db.flush();db.add(MachineTemplate(room_template_id=t.id,source_builder_environment_id=bid,status='creating'));db.commit()
        response=env.client.post(f'/api/rooms/{env.room}/builder/creator-key',headers=env.headers['creator']);assert response.status_code==409
        assert guest.call_count==1
        with env.maker() as db:db.get(Room,env.room).room_type='THEORY';db.commit()
        assert env.client.post(f'/api/rooms/{env.room}/builder/creator-key',headers=env.headers['creator']).status_code==409


def test_metadata_reader_uses_only_fixed_imdsv2_identity_endpoints():
    import io
    from app.creator_key_guest import BOOT_HELPER
    namespace={'__name__':'isolated_helper'};exec(BOOT_HELPER,namespace)
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append((request.full_url,request.get_method(),request.headers,timeout))
            return io.BytesIO(b'isolated-token' if request.get_method()=='PUT' else b'i-0123456789abcdef0')
    with patch('urllib.request.build_opener',return_value=Opener()):assert namespace['instance_identity']()=='i-0123456789abcdef0'
    assert calls[0][0]=='http://169.254.169.254/latest/api/token' and calls[0][1]=='PUT'
    assert calls[1][0]=='http://169.254.169.254/latest/meta-data/instance-id' and calls[1][1]=='GET'
    assert calls[1][2]['X-aws-ec2-metadata-token']=='isolated-token'
    assert all(c[-1]==3 for c in calls)


def test_remote_transport_refuses_production_or_mismatched_instance_tags(env):
    from types import SimpleNamespace
    from app.creator_keys import run_guest
    bid=builder(env)
    with env.maker() as db:
        target=db.get(BuilderEnvironment,bid);target.instance_id='i-0123456789abcdef0';target.private_ip='10.0.1.10'
        registered=SimpleNamespace(revision=1,public_key=key(),enabled=True)
        with patch('app.ami_manager.AMIManager') as manager,patch('app.vpn.VpnManager') as transport:
            manager.return_value.validate_source_instance.return_value=target.private_ip
            manager.return_value.ec2_client.describe_instances.return_value={'Reservations':[{'Instances':[{'InstanceId':target.instance_id,'State':{'Name':'running'},'PrivateIpAddress':target.private_ip,'Tags':[{'Key':'ResourceType','Value':'production'}]}]}]}
            with pytest.raises(ValueError,match='identity mismatch'):run_guest(target,registered)
            transport.assert_not_called()


def test_automatic_setup_without_registered_key_never_calls_ssh(env):
    from app.creator_keys import automatic_key
    bid=builder(env)
    with env.maker() as db,patch('app.creator_keys.run_guest') as guest:
        automatic_key(db,db.get(BuilderEnvironment,bid),db.get(User,env.users['creator']));guest.assert_not_called()


def test_rotation_tracks_old_guests_and_bounds_ssh_operations_per_retry(env):
    from datetime import datetime,timedelta
    from app.models import CreatorBuilderKey
    bid=builder(env)
    with patch('app.creator_keys.run_guest'):
        assert save(env).status_code==200
    with env.maker() as db:
        old=BuilderEnvironment(room_id=env.room,creator_id=env.users['creator'],status='stopped',instance_id='i-0123456789abcdef1',private_ip='10.0.1.23',terraform_state_path='offline-old',expires_at=datetime.utcnow()+timedelta(hours=1));db.add(old);db.flush();old_id=old.id
        db.add(CreatorBuilderKey(builder_id=old.id,user_id=env.users['creator'],revision=1,enabled=True));db.commit()
    new=key()
    seen=[]
    with patch('app.creator_keys.gone_instances',return_value=set()),patch('app.creator_keys.run_guest',side_effect=lambda builder,desired:seen.append((builder.id,desired.enabled))) as guest:
        first=save(env,new,1);assert first.status_code==200 and first.json()['pending']
        assert guest.call_count==1
        second=save(env,new,2);assert second.status_code==200 and not second.json()['pending']
        assert guest.call_count==2
        # One guest gets the replacement; the historical one is retired.
        desired=dict(seen)
        assert desired=={bid:True,old_id:False}
    with env.maker() as db:assert not db.get(CreatorBuilderKey,old_id).enabled and db.get(CreatorBuilderKey,bid).enabled


def test_offline_revocation_stays_pending_until_guest_is_verified_gone(env):
    from app.models import CreatorBuilderKey
    bid=builder(env)
    with patch('app.creator_keys.run_guest'):save(env)
    with env.maker() as db:db.get(BuilderEnvironment,bid).status='stopped';db.commit()
    with patch('app.creator_keys.gone_instances',return_value=set()),patch('app.creator_keys.run_guest',side_effect=RuntimeError('offline')):
        removed=env.client.request('DELETE','/api/users/me/creator-key',headers=env.headers['creator'],json={'revision':1})
        assert removed.status_code==200 and removed.json()['pending']
    with env.maker() as db:
        host=db.get(BuilderEnvironment,bid).instance_id
        assert db.get(CreatorBuilderKey,bid).enabled
    with patch('app.creator_keys.gone_instances',return_value={host}),patch('app.creator_keys.run_guest') as guest:
        retry=env.client.request('DELETE','/api/users/me/creator-key',headers=env.headers['creator'],json={'revision':2})
        assert retry.status_code==200 and not retry.json()['pending'];guest.assert_not_called()
    with env.maker() as db:assert not db.get(CreatorBuilderKey,bid).enabled


def test_disabled_registry_never_grants_access_or_adds_setup_to_a_new_builder(env):
    from app.creator_keys import automatic_key
    save(env)
    with env.maker() as db:
        registered=db.get(CreatorSshKey,env.users['creator']);registered.enabled=False;db.commit()
    bid=builder(env)
    with env.maker() as db,patch('app.creator_keys.run_guest') as guest:
        automatic_key(db,db.get(BuilderEnvironment,bid),db.get(User,env.users['creator']));guest.assert_not_called()
