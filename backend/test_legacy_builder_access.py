"""Live app retires personal-key APIs; existing EC2 PEM and learner auth stay intact."""
from pathlib import Path
from unittest.mock import patch
import pytest
from sqlalchemy import select,func
from app.main import app
from app.models import BuilderEnvironment,CreatorSshKey,AdminAudit
from app.lab_manager import LabManager
from app.config import settings
from test_challenges import env

@pytest.mark.parametrize('method,path',[
 ('get','/api/users/me/creator-key'),('put','/api/users/me/creator-key'),('delete','/api/users/me/creator-key'),
 ('post','/api/rooms/1/builder/creator-key'),('get','/api/admin/users/1/creator-key'),('post','/api/admin/users/1/creator-key/revoke')])
def test_retired_personal_key_routes_are_not_mounted_or_executable(env,monkeypatch,method,path):
    monkeypatch.setattr('app.creator_keys.run_guest',lambda *a:pytest.fail('Retired API reached guest'))
    monkeypatch.setattr('boto3.client',lambda *a,**k:pytest.fail('Retired API reached AWS'))
    for actor in [None,'creator','other','learner','admin']:
        response=env.client.request(method,path,headers=env.headers[actor] if actor else {},json={'public_key':'synthetic-public-only','revision':0})
        assert response.status_code==404
    with env.maker() as db:
        assert db.scalar(select(func.count()).select_from(CreatorSshKey))==0
        assert db.scalar(select(func.count()).select_from(AdminAudit))==0
    assert not any(getattr(route,'path','')==path for route in app.routes)


def test_future_builder_uses_configured_ec2_key_without_personal_registration(env):
    def provision(builder):
        builder.status='running';builder.instance_id='i-isolated-new';builder.private_ip='10.0.1.50'
    with patch('app.builder.lab_manager.start_builder',side_effect=provision),patch('app.builder.authorize_builder_access'),patch('app.builder.sync_backend_routes'),patch('app.creator_keys.automatic_key',side_effect=AssertionError('Retired setup ran')) as personal:
        r=env.client.post(f'/api/rooms/{env.room}/builder',headers=env.headers['creator'],json={'ttl_hours':1})
        assert r.status_code==201,r.text
        personal.assert_not_called()
    with env.maker() as db:
        builder=db.get(BuilderEnvironment,r.json()['id'])
        assert builder.status=='running' and builder.ssh_auth_mode=='key'
        assert f'ssh_key_name     = "{settings.builder_ssh_key_name}"' in LabManager()._generate_tfvars(builder)
    source=Path(__file__).parent.parent/'infrastructure/terraform/builder/modules/builder_env/instance.tf'
    text=source.read_text();assert 'key_name                    = var.ssh_key_name' in text
    assert 'associate_public_ip_address = false' in text
    assert 'PRIVATE KEY' not in text

@pytest.mark.parametrize('actor,status',[('learner',403),('other',404),('admin',404)])
def test_builder_ownership_and_learner_auth_routes_remain_protected(env,actor,status):
    from test_builder_ssh_auth import builder
    builder(env)
    assert env.client.get(f'/api/rooms/{env.room}/builder',headers=env.headers[actor]).status_code==status
    assert env.client.post(f'/api/rooms/{env.room}/builder/ssh-auth',headers=env.headers[actor],json={'mode':'key','username':'training'}).status_code==status
