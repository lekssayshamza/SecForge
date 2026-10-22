"""Isolated administrative actions, privacy and accurate creator analytics."""
from datetime import datetime,timedelta
import json,importlib.util
from pathlib import Path
import pytest
from sqlalchemy import select,func,text,event
from app.models import User,Room,AdminControl,AdminAudit,LearningPath,LearningPathRoom,ChallengeQuestion,QuestionSolve,RoomProgress,LabInstance,BuilderEnvironment,RoomTemplate,MachineTemplate
from app.operations_migration import migrate
from app.security import create_access_token,hash_password
from test_room_tasks import env
@pytest.fixture
def ops(env):
    with env.maker() as db:
        db.add(AdminControl(id=1));db.add(User(id=10,username='secondadmin',email='secondadmin@example.com',password_hash='unused',role='admin'));db.commit()
    return env

def change(e,uid=None,**patch):
    data=dict(expected_role='learner',expected_suspended=False,confirm_username='learner',reason='access-change',role='creator');data.update(patch)
    return e.client.patch('/api/admin/users/'+str(uid or e.ids['learner']),headers=e.header('admin'),json=data)
@pytest.mark.parametrize('path',['/api/admin','/api/admin/users','/api/admin/content/rooms','/api/admin/content/paths','/api/admin/labs','/api/admin/audit','/api/admin/infrastructure'])
def test_admin_gates_guest_learner_creator(ops,path):
    for h in [{},ops.header('learner'),ops.header('owner')]:assert ops.client.get(path,headers=h).status_code in (401,403)
@pytest.mark.parametrize('name',['learner'])
def test_analytics_creator_only(ops,name):assert ops.client.get('/api/creator/analytics',headers=ops.header(name)).status_code==403

def test_admin_lists_pagination_privacy_and_no_delete_routes(ops):
    e=ops;r=e.client.get('/api/admin/users?q=own&page_size=1',headers=e.header('admin'));assert r.status_code==200 and r.json()['total']==1
    item=r.json()['items'][0];assert set(item)=={'id','username','display_name','role','suspended','created_at','points','completed_rooms'}
    assert e.client.get('/api/admin/users?page_size=51',headers=e.header('admin')).status_code==422
    assert e.client.get('/api/admin/users/999',headers=e.header('admin')).status_code==404
    for path in ['/api/admin/labs/1','/api/admin/audit/1','/api/admin/users/1']:
        assert e.client.delete(path,headers=e.header('admin')).status_code in (404,405)
    assert e.client.get('/api/admin/labs?status=all',headers=e.header('admin')).json()['items']==[]

def test_role_change_audit_invalidation_and_self_change_rejected(ops):
    e=ops;old=e.header('learner');assert change(e).json()=={'changed':True}
    assert e.client.get('/api/auth/me',headers=old).status_code==401
    with e.maker() as db:
        u=db.get(User,e.ids['learner']);assert u.role=='creator' and u.token_version==1
        row=db.scalar(select(AdminAudit));assert row.actor_id==e.ids['admin'] and row.action=='role-change' and row.outcome=='success'
    assert change(e,e.ids['admin'],expected_role='admin',confirm_username='admin').status_code==409
    assert change(e,role='admin',expected_role='creator',confirm_username='incorrect').status_code==409
    assert e.client.get('/api/admin/audit',headers=e.header('admin')).json()['total']==3

def test_suspension_login_google_and_reactivation(ops):
    e=ops
    with e.maker() as db:
        u=db.get(User,e.ids['learner']);u.password_hash=hash_password('Synthetic-password-123');db.commit()
    token=e.header('learner');assert change(e,role=None,suspended=True).status_code==200
    assert e.client.get('/api/auth/me',headers=token).status_code==403
    assert e.client.post('/api/auth/login',json={'username':'learner','password':'Synthetic-password-123'}).status_code==401
    from app.auth import account_allowed
    with e.maker() as db:assert not account_allowed(db.get(User,e.ids['learner']))
    assert change(e,role=None,suspended=False,expected_suspended=True).status_code==200
    assert e.client.get('/api/auth/me',headers=token).status_code==401
    assert e.client.post('/api/auth/login',json={'username':'learner','password':'Synthetic-password-123'}).status_code==200

def test_active_resource_blocks_access_change_without_termination(ops):
    e=ops
    with e.maker() as db:db.add(LabInstance(room_id=1,learner_id=e.ids['learner'],status='running',instance_id='i-synthetic',expires_at=datetime.utcnow()+timedelta(hours=1)));db.commit()
    assert change(e,role=None,suspended=True).status_code==409
    with e.maker() as db:assert not db.get(User,e.ids['learner']).suspended and db.scalar(select(LabInstance.status))=='running'

def moderation(e,publish=False,kind='rooms',id=1,**patch):
    data=dict(publish=publish,confirmation=('PUBLISH ' if publish else 'UNPUBLISH ')+str(id),reason='content-review');data.update(patch)
    return e.client.post(f'/api/admin/content/{kind}/{id}/moderate',headers=e.header('admin'),json=data)
def test_moderation_holds_publication_and_uses_original_rules(ops):
    e=ops
    with e.maker() as db:db.get(Room,1).room_type='THEORY';db.get(Room,1).status='published';db.commit()
    assert moderation(e).status_code==200
    assert e.client.post('/api/rooms/1/publish',headers=e.header('owner')).status_code==409
    assert moderation(e,True).status_code==200
    assert e.client.get('/api/public/rooms/1').status_code==200
    assert moderation(e,id=2,publish=True).status_code==409 # practical has no saved image
    with e.maker() as db:
        assert db.get(Room,2).status=='draft'
        assert [r.outcome for r in db.scalars(select(AdminAudit).order_by(AdminAudit.id))]==['success','success','denied']

def test_path_moderation_rejects_private_members_and_keeps_ownership(ops):
    e=ops
    with e.maker() as db:
        p=LearningPath(id=1,creator_id=e.ids['other'],title='Owned path',description='',category='linux',difficulty='easy');db.add(p);db.flush();db.add(LearningPathRoom(path_id=1,room_id=2,display_order=1));db.commit()
    assert moderation(e,True,'paths').status_code==409
    assert e.client.patch('/api/rooms/2',headers=e.header('admin'),json={'name':'Forbidden'}).status_code==404
    assert e.client.post('/api/rooms/2/questions',headers=e.header('admin'),json={'question_text':'Forbidden','answer':'test'}).status_code==404

def test_metrics_authoritative_ownership_zero_data_and_points(ops):
    e=ops
    r=e.client.get('/api/creator/analytics',headers=e.header('owner'));assert r.status_code==200,r.text
    assert r.json()['views'] is None and r.json()['room_starts']==0 and r.json()['completion_rate'] is None
    with e.maker() as db:
        db.get(Room,1).status='published';db.get(Room,1).room_type='THEORY'
        q=ChallengeQuestion(room_id=1,question_text='Private question',answer_salt='x'*32,answer_digest='a'*64,points=75,display_order=1);db.add(q);db.flush()
        db.add(QuestionSolve(question_id=q.id,learner_id=e.ids['learner'],points_awarded=75));db.add(RoomProgress(room_id=1,learner_id=e.ids['learner'],attempt_count=1,completed_at=datetime.utcnow(),points_earned=900))
        db.add(RoomProgress(room_id=2,learner_id=e.ids['learner'],attempt_count=1,completed_at=datetime.utcnow(),points_earned=999)) # another owner; no proof
        p=LearningPath(id=1,creator_id=e.ids['owner'],title='Path',description='',category='linux',difficulty='easy',status='published');db.add(p);db.flush();db.add(LearningPathRoom(path_id=1,room_id=1,display_order=1));db.commit()
    v=e.client.get('/api/creator/analytics',headers=e.header('owner')).json();assert v['points']==75 and v['solves']==1 and v['completions']==1 and v['completion_rate']==100 and v['theory_rooms']==1 and v['paths'][0]['learners_completed']==1
    assert e.client.get('/api/creator/analytics/rooms/2',headers=e.header('owner')).status_code==404
    detail=e.client.get('/api/creator/analytics/rooms/1',headers=e.header('owner')).json();assert detail['questions'][0]['solve_count']==1
    assert 'answer_digest' not in str(detail) and 'learner_id' not in str(detail)
    stats=e.client.get('/api/users/me/learning',headers=e.header('learner')).json()['stats'];assert stats['points']==v['points']

def test_migration_rerun_preserves_rows_no_promotion(ops):
    e=ops
    with e.engine.begin() as c:
        before=[tuple(r) for r in c.execute(text('SELECT id,username,role,token_version FROM users ORDER BY id'))]
        assert migrate(c);assert not migrate(c)
        assert [tuple(r) for r in c.execute(text('SELECT id,username,role,token_version FROM users ORDER BY id'))]==before

def test_inventory_is_read_only_safe_cached_and_unavailable_is_honest(ops,monkeypatch):
    e=ops;import app.infrastructure_visibility as infra
    monkeypatch.setattr(infra,'_cache',None);monkeypatch.setattr(infra,'_expires',0)
    calls=[]
    class EC2:
        def describe_instances(self,**kw):calls.append(kw);return {'Reservations':[{'Instances':[{'InstanceId':'i-fixture','InstanceType':'t3.small','State':{'Name':'running'},'Tags':[{'Key':'Name','Value':'SecForge App'}]}]}]}
    import boto3;monkeypatch.setattr(boto3,'client',lambda *a,**k:EC2())
    for _ in range(2):
        v=e.client.get('/api/admin/infrastructure',headers=e.header('admin')).json();assert v['available'] and v['hourly_estimate'] is None and v['instances'][0]['role']=='application (tag identified)'
    assert len(calls)==1
    monkeypatch.setattr(infra,'_expires',0);monkeypatch.setattr(boto3,'client',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('sensitive diagnostics')))
    v=e.client.get('/api/admin/infrastructure',headers=e.header('admin')).json();assert not v['available'] and 'sensitive diagnostics' not in str(v)

def test_bootstrap_is_explicit_first_only_and_audited(ops):
    e=ops;spec=importlib.util.spec_from_file_location('bootstrap',Path(__file__).parent/'deploy/cloud/bootstrap_admin.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with e.maker() as db:
        for u in db.scalars(select(User).where(User.role=='admin')):u.role='learner'
        db.commit()
        with pytest.raises(ValueError):module.bootstrap(db,e.ids['owner'],'wrong','PROMOTE 1 wrong')
        assert db.get(User,e.ids['owner']).role=='creator'
        module.bootstrap(db,e.ids['owner'],'owner','PROMOTE '+str(e.ids['owner'])+' owner')
        assert db.get(User,e.ids['owner']).role=='admin'
        assert db.scalar(select(AdminAudit.action))=='first-admin-bootstrap'
        with pytest.raises(ValueError):module.bootstrap(db,e.ids['other'],'other','PROMOTE 2 other')


def test_client_role_claim_never_confers_admin_and_sensitive_fields_rejected(ops):
    e=ops;forged={'Authorization':'Bearer '+create_access_token(e.ids['learner'],'admin')}
    assert e.client.get('/api/admin',headers=forged).status_code==403
    assert e.client.patch('/api/admin/users/3',headers=e.header('admin'),json={'role':'admin','email':'private@example.com'}).status_code==422
    assert e.client.get('/api/admin/audit',headers=e.header('owner')).status_code==403
    assert e.client.get('/api/admin/users?q=%27%20OR%201%3D1--',headers=e.header('admin')).json()['total']==0


def test_lab_monitoring_redacts_failure_details_and_uses_authoritative_deadline(ops):
    e=ops
    with e.maker() as db:
        db.add(LabInstance(room_id=1,learner_id=e.ids['learner'],status='failed',instance_id='i-fixture',terraform_state_path='private/path',private_ip='10.0.1.99',error_message='secret token and private key must never be returned',expires_at=datetime.utcnow()-timedelta(minutes=2)));db.commit()
    r=e.client.get('/api/admin/labs?status=failed',headers=e.header('admin'));assert r.status_code==200
    row=r.json()['items'][0];assert row['remaining_seconds']==0 and row['cleanup_status']=='retry pending'
    assert not any(s in r.text for s in ['private key','secret token','private/path','10.0.1.99','instance_id','terraform_state_path'])


def test_ledger_credit_survives_role_change_and_unpublication(ops):
    e=ops
    with e.maker() as db:
        q=ChallengeQuestion(room_id=1,question_text='Question',answer_salt='s'*32,answer_digest='d'*64,points=50,display_order=1);db.add(q);db.flush();db.add(QuestionSolve(question_id=q.id,learner_id=e.ids['learner'],points_awarded=50));db.add(RoomProgress(room_id=1,learner_id=e.ids['learner'],completed_at=datetime.utcnow(),points_earned=50));db.commit()
    assert change(e).status_code==200
    d=e.client.get('/api/creator/analytics',headers=e.header('owner')).json();assert d['solves']==1 and d['points']==50 and d['completions']==1
    assert e.client.get('/api/admin/users/'+str(e.ids['learner']),headers=e.header('admin')).json()['points']==50


def test_creator_preview_does_not_count_as_start_and_unproven_completion_not_counted(ops):
    e=ops
    with e.maker() as db:
        db.add(LabInstance(room_id=1,learner_id=e.ids['owner'],status='stopped',expires_at=datetime.utcnow()));db.add(RoomProgress(room_id=1,learner_id=e.ids['learner'],completed_at=datetime.utcnow(),points_earned=999));db.commit()
    d=e.client.get('/api/creator/analytics',headers=e.header('owner')).json();assert d['total_starts']==d['points']==d['completions']==0


def test_analytics_and_admin_reads_are_bounded_without_per_item_queries(ops):
    e=ops;queries=[]
    def observe(*args):queries.append(args[2])
    event.listen(e.engine,'before_cursor_execute',observe)
    for url,actor in [('/api/creator/analytics','owner'),('/api/creator/analytics/rooms','owner'),('/api/admin','admin'),('/api/admin/users','admin')]:
        queries.clear();r=e.client.get(url,headers=e.header(actor));assert r.status_code==200,r.text
        assert len(queries)<=10,(url,len(queries))
