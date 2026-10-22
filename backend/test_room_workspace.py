"""Unpublished working copies, immutable credit, image pins and stale editors."""
from copy import deepcopy
from unittest.mock import patch
import pytest
from sqlalchemy import select, func, text
from test_challenges import env, add, publish, submit, progress
from app.models import (Room, RoomDraft, RoomRelease, RoomTask, RoomTemplate,
                        MachineTemplate, ChallengeQuestion, QuestionSolve, RoomProgress)
from app.workspace_migration import migrate


def read(e):
    r = e.client.get(f'/api/rooms/{e.room}/workspace', headers=e.headers['creator'])
    assert r.status_code == 200, r.text
    return r.json()


def edit(e, action, data, revision=None):
    return e.client.patch(f'/api/rooms/{e.room}/workspace', headers=e.headers['creator'],
                         json={'revision': read(e)['revision'] if revision is None else revision, 'action': action, 'data': data})


def release(e, revision=None):
    return e.client.post(f'/api/rooms/{e.room}/workspace/publish', headers=e.headers['creator'],
                         json={'revision': read(e)['revision'] if revision is None else revision})


def theory(e):
    with e.maker() as db:
        db.get(Room, e.room).room_type = 'THEORY'
        db.commit()


def test_workspace_get_is_read_only_and_answers_never_returned(env):
    add(env)
    r = read(env)
    assert r['revision'] == 0 and not r['dirty'] and r['questions'][0]['has_saved_answer']
    assert all(secret not in str(r) for secret in ['answer_digest', 'answer_salt', 'SF{offline-only}'])
    with env.maker() as db: assert db.get(RoomDraft, env.room) is None


@pytest.mark.parametrize('role,status', [('learner',403), ('other',404), ('admin',404)])
def test_workspace_ownership_all_methods(env, role, status):
    url = f'/api/rooms/{env.room}/workspace'
    for method, path, data in [('get',url,None), ('patch',url,{'revision':0,'action':'metadata','data':{'name':'Wrong'}}), ('post',url+'/publish',{'revision':0})]:
        r = getattr(env.client,method)(path,headers=env.headers[role],**({'json':data} if data is not None else {}))
        assert r.status_code == status
    assert env.client.get(url).status_code in (401,403)


def test_draft_editing_publish_is_atomic_and_idempotent(env):
    q = add(env).json()['id']; publish(env)
    before = env.client.get(f'/api/public/rooms/{env.room}').json()
    assert edit(env,'metadata',{'name':'Private revision'}).status_code == 200
    assert edit(env,'question_update',{'id':q,'hint':'Private hint'}).status_code == 200
    assert env.client.get(f'/api/public/rooms/{env.room}').json() == before
    assert env.client.get(f'/api/rooms/{env.room}/questions', headers=env.headers['learner']).json()['questions'][0]['hint'] is None
    revision = read(env)['revision']
    assert release(env,revision).status_code == 200
    assert release(env,revision).status_code == 200
    assert env.client.get(f'/api/public/rooms/{env.room}').json()['name'] == 'Private revision'
    with env.maker() as db:
        assert db.scalar(select(func.count()).select_from(RoomRelease)) == 2
        assert db.get(ChallengeQuestion,q).hint == 'Private hint'
        snapshots=list(db.scalars(select(RoomRelease).order_by(RoomRelease.id)))
        assert snapshots[0].document['metadata']['name'] == before['name']


def test_stale_edit_and_stale_publish_rejected_without_data_loss(env):
    theory(env)
    assert edit(env,'metadata',{'name':'First'},0).status_code == 200
    assert edit(env,'metadata',{'name':'Stale'},0).status_code == 409
    assert release(env,0).status_code == 409
    assert read(env)['room']['name'] == 'First'
    assert release(env).status_code == 200


def test_saved_answer_kept_replaced_and_empty_rejected(env):
    q=add(env).json()['id']; publish(env)
    with env.maker() as db: original=db.get(ChallengeQuestion,q).answer_digest
    assert edit(env,'question_update',{'id':q,'question_text':'Clearer wording'}).status_code == 200
    assert release(env).status_code == 200
    with env.maker() as db: assert db.get(ChallengeQuestion,q).answer_digest == original
    assert submit(env,q).json()['points_awarded'] == 100
    before=read(env)
    for answer in ['', ' ', None]:
        assert edit(env,'question_update',{'id':q,'answer':answer}).status_code == 422
    assert read(env)==before
    assert edit(env,'question_update',{'id':q,'answer':'synthetic-new-answer'}).status_code == 200
    assert submit(env,q).json()['points_awarded'] == 0
    assert release(env).status_code == 200
    new=read(env)['questions'][0]['id'];assert new != q
    assert submit(env,q).status_code == 404
    assert submit(env,new,'synthetic-new-answer').json()['points_awarded'] == 100
    with env.maker() as db:
        assert not db.get(ChallengeQuestion,q).is_active
        assert db.get(ChallengeQuestion,q).answer_digest == original
        assert db.scalar(select(func.sum(QuestionSolve.points_awarded))) == 200


def test_added_removed_questions_and_points_preserve_historical_credit(env):
    q=add(env).json()['id'];publish(env);before=submit(env,q).json()['progress']
    assert edit(env,'question_create',{'question_text':'New question','answer':'new','points':50}).status_code == 200
    assert edit(env,'question_update',{'id':q,'points':75}).status_code == 200
    assert release(env).status_code == 200
    p=progress(env)
    assert p['completed_at']==before['completed_at'] and p['points_earned']==100 and p['questions_solved']==0
    qs=read(env)['questions'];newq=qs[1]['id']
    assert submit(env,newq,'new').json()['points_awarded']==50
    assert edit(env,'question_remove',{'id':qs[0]['id']}).status_code == 200
    assert release(env).status_code == 200
    assert progress(env)['points_earned']==150 and progress(env)['completed_at']==before['completed_at']
    assert submit(env,newq,'new').json()['points_awarded']==0
    with env.maker() as db:
        assert db.scalar(select(QuestionSolve.release_id).where(QuestionSolve.question_id==newq)) is not None


def test_tasks_and_questions_temporary_ids_order_and_preview(env):
    theory(env)
    t=edit(env,'task_create',{'title':'First lesson','instructions':'## Private lesson'}).json()['tasks'][0]['id']
    assert t<0
    q=edit(env,'question_create',{'question_text':'Read the lesson','answer':'synthetic','task_id':t}).json()['questions'][0]['id']
    t2=edit(env,'task_create',{'title':'Second lesson'}).json()['tasks'][1]['id']
    assert edit(env,'task_order',{'ids':[t2,t]}).status_code == 200
    assert edit(env,'question_order',{'ids':[q,q]}).status_code == 422
    assert release(env).status_code == 200
    state=read(env);assert state['tasks'][0]['title']=='Second lesson' and all(t['id']>0 for t in state['tasks'])
    assert state['questions'][0]['task_id']==state['tasks'][1]['id']
    assert edit(env,'task_remove',{'id':state['tasks'][1]['id']}).status_code == 200
    assert release(env).status_code == 200
    assert read(env)['questions'][0]['task_id'] is None


def test_publish_failure_rolls_back_and_failed_image_never_falls_back(env):
    q=add(env).json()['id'];publish(env)
    assert edit(env,'metadata',{'name':'Unpublished'}).status_code == 200
    with env.maker() as db:
        draft=db.get(RoomDraft,env.room)
        template=RoomTemplate(room_id=env.room,created_by=env.users['creator'],version=2);db.add(template);db.flush()
        db.add(MachineTemplate(room_template_id=template.id,status='failed'))
        draft.image_template_id=template.id;db.commit()
    assert release(env).status_code == 409
    with env.maker() as db:
        assert db.get(Room,env.room).name!='Unpublished'
        assert db.scalar(select(RoomTemplate.version).where(RoomTemplate.is_published.is_(True)))==1
        draft=db.get(RoomDraft,env.room);draft.image_template_id=db.scalar(select(RoomTemplate.id).where(RoomTemplate.version==1));db.commit()
    with patch('app.room_workspace.install_content',side_effect=RuntimeError('isolated failure')):
        with pytest.raises(RuntimeError):release(env)
    assert read(env)['dirty']
    with env.maker() as db:assert db.get(Room,env.room).name!='Unpublished'


def test_failure_after_content_install_restores_entire_live_revision(env):
    q=add(env).json()['id'];publish(env)
    with env.maker() as db:
        original=db.get(ChallengeQuestion,q).answer_digest
        releases=db.scalar(select(func.count()).select_from(RoomRelease))
    assert edit(env,'metadata',{'name':'Must not leak'}).status_code==200
    assert edit(env,'question_update',{'id':q,'answer':'synthetic-new-rubric','points':75}).status_code==200
    with patch('app.room_workspace.document',side_effect=RuntimeError('After live writes, before activation')):
        with pytest.raises(RuntimeError):release(env)
    with env.maker() as db:
        assert db.get(Room,env.room).name!='Must not leak'
        assert db.get(ChallengeQuestion,q).is_active and db.get(ChallengeQuestion,q).answer_digest==original
        assert db.scalar(select(func.count()).select_from(ChallengeQuestion))==1
        assert db.scalar(select(func.count()).select_from(RoomRelease))==releases
        assert db.scalar(select(RoomTemplate.version).where(RoomTemplate.is_published.is_(True)))==1
        assert db.get(RoomDraft,env.room).dirty


@pytest.mark.parametrize('action,data', [
    ('metadata',{'room_type':'THEORY'}),('metadata',{'creator_id':999}),
    ('metadata',{'category':'arbitrary-category'}),('metadata',{'name':None}),
    ('question_create',{'question_text':'x','answer':'x','answer_digest':'forged'}),
    ('task_create',{'title':'x','instructions':'bad\x00'}),
])
def test_invalid_draft_data_and_secrets_not_echoed(env,action,data):
    r=edit(env,action,data);assert r.status_code in (400,422)
    assert 'forged' not in r.text and 'answer_digest' not in r.text
    assert read(env)['revision']==0


def test_legacy_mutations_cannot_bypass_draft_or_published_isolation(env):
    q=add(env).json()['id'];publish(env)
    assert env.client.patch(f'/api/rooms/{env.room}/questions/{q}',headers=env.headers['creator'],json={'hint':'bypass'}).status_code==409
    assert env.client.post(f'/api/rooms/{env.room}/tasks',headers=env.headers['creator'],json={'title':'bypass'}).status_code==409
    assert edit(env,'metadata',{'name':'Working copy'}).status_code==200
    assert env.client.post(f'/api/rooms/{env.room}/unpublish',headers=env.headers['creator']).status_code==200
    assert env.client.post(f'/api/rooms/{env.room}/templates/1/publish',headers=env.headers['creator']).status_code==409


def test_migration_rerun_preserves_existing_rows(env):
    add(env)
    with env.maker() as db:
        before=[tuple(row) for row in db.execute(text('SELECT * FROM challenge_questions'))]
        assert migrate(db.connection()) and not migrate(db.connection())
        assert [tuple(row) for row in db.execute(text('SELECT * FROM challenge_questions'))]==before
        assert db.scalar(select(func.count()).select_from(RoomRelease))==0
        db.commit()


def test_update_builder_uses_current_published_image(env):
    from app.models import BuilderEnvironment
    from app import builder as module
    add(env);publish(env)
    observed=[]
    from datetime import datetime,timedelta
    with env.maker() as db:
        source=BuilderEnvironment(room_id=env.room,creator_id=env.users['creator'],status='stopped',terraform_state_path='retained',expires_at=datetime.utcnow()-timedelta(hours=1),ssh_auth_mode='password',ssh_guest_username='training')
        db.add(source);db.flush()
        db.scalar(select(MachineTemplate)).source_builder_environment_id=source.id;db.commit()
    def provision(b):
        observed.append(b.source_ami_id);b.status='running';b.instance_id='i-isolated';b.private_ip='10.0.1.50'
    with patch.object(module.lab_manager,'start_builder',side_effect=provision),patch.object(module,'authorize_builder_access'),patch.object(module,'sync_backend_routes'):
        r=env.client.post(f'/api/rooms/{env.room}/builder',headers=env.headers['creator'],json={})
        assert r.status_code==201 and observed==['ami-isolated-fixture']
    with env.maker() as db:
        b=db.scalar(select(BuilderEnvironment).where(BuilderEnvironment.status=='running'));assert b.source_ami_id=='ami-isolated-fixture'
        assert b.ssh_auth_mode=='password' and b.ssh_guest_username=='training'
        textvars=module.lab_manager._generate_tfvars(b)
        assert 'ami-isolated-fixture' in textvars


def test_published_builder_does_not_fall_back_to_unpinned_image(env):
    from app import builder as module
    publish(env)
    with env.maker() as db:
        db.scalar(select(RoomTemplate)).is_published=False;db.commit()
    with patch.object(module.lab_manager,'start_builder') as provision:
        assert env.client.post(f'/api/rooms/{env.room}/builder',headers=env.headers['creator'],json={}).status_code==409
        provision.assert_not_called()


@pytest.mark.parametrize('history',['image','publication','running_builder','stopped_builder','learner_lab'])
def test_delete_never_deregisters_images_or_erases_resource_history(env,history):
    from datetime import datetime,timedelta
    from app.models import BuilderEnvironment,LabInstance
    with env.maker() as db:
        if history=='image':
            t=RoomTemplate(room_id=env.room,created_by=env.users['creator'],version=1);db.add(t);db.flush()
            db.add(MachineTemplate(room_template_id=t.id,ami_id='ami-retained',status='available'))
        elif history=='publication':db.add(RoomRelease(room_id=env.room,published_by=env.users['creator'],document={}))
        elif history.endswith('builder'):db.add(BuilderEnvironment(room_id=env.room,creator_id=env.users['creator'],status='running' if history=='running_builder' else 'stopped',terraform_state_path='retained',expires_at=datetime.utcnow()+timedelta(hours=1)))
        else:db.add(LabInstance(room_id=env.room,learner_id=env.users['learner'],status='running',expires_at=datetime.utcnow()+timedelta(hours=1),terraform_state_path='retained'))
        db.commit()
    with patch('app.ami_manager.AMIManager') as aws:
        r=env.client.delete(f'/api/rooms/{env.room}',headers=env.headers['creator'])
        assert r.status_code==409 and 'preserved' in r.text;aws.assert_not_called()
    with env.maker() as db:assert db.get(Room,env.room) is not None


def test_unused_draft_deletion_cascades_its_working_copy_without_aws_calls(env):
    assert edit(env,'metadata',{'name':'Unused working copy'}).status_code==200
    with patch('app.ami_manager.AMIManager') as aws:
        assert env.client.delete(f'/api/rooms/{env.room}',headers=env.headers['creator']).status_code==204
        aws.assert_not_called()
    with env.maker() as db:assert db.get(RoomDraft,env.room) is None
