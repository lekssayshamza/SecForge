"""Real PostgreSQL lock waits; no AWS or external database targets permitted."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned PostgreSQL concurrency runner', allow_module_level=True)

from fastapi import BackgroundTasks
from sqlalchemy import select, func, text
from app.models import (User, Room, RoomDraft, RoomRelease, RoomTemplate, MachineTemplate,
                        QuestionSolve, RoomProgress, ChallengeQuestion, LabInstance)
from app.room_workspace import edit_workspace, publish_workspace, WorkspaceWrite, PublishWrite
from app.challenges import submit_answer
from app.schemas import AnswerSubmission, LabStartRequest
from app.labs import start_lab
from app.workspace_migration import migrate
from test_postgres_challenges import seed, interleave
from test_postgres_lab_concurrency import pg_sessions


def prepare(maker, practical=False):
    room,creator,learner,_,question,_=seed(maker)
    with maker() as db:
        db.get(Room,room).room_type='PRACTICAL' if practical else 'THEORY'
        if practical:
            a=RoomTemplate(room_id=room,created_by=creator,version=1,is_published=True)
            b=RoomTemplate(room_id=room,created_by=creator,version=2,is_published=False)
            db.add_all([a,b]);db.flush()
            db.add_all([MachineTemplate(room_template_id=t.id,ami_id=ami,status='available') for t,ami in [(a,'ami-old-fixture'),(b,'ami-new-fixture')]])
        db.commit()
    return room,creator,learner,question


def edit(db,room,user,action,data,revision=0):
    return edit_workspace(room,WorkspaceWrite(revision=revision,action=action,data=data),db.get(User,user),db)


def publish(db,room,user,revision):
    return publish_workspace(room,PublishWrite(revision=revision),db.get(User,user),db)


def test_concurrent_edit_rejects_stale_writer(pg_sessions):
    room,creator,_,_=prepare(pg_sessions)
    r=interleave(pg_sessions,lambda db:edit(db,room,creator,'metadata',{'name':'First'}),lambda db:edit(db,room,creator,'metadata',{'name':'Stale'}))
    assert r['first'][0]==200 and r['second'][0]==409
    with pg_sessions() as db:assert db.get(RoomDraft,room).document['metadata']['name']=='First'


def test_duplicate_publish_is_one_activation(pg_sessions):
    room,creator,_,_=prepare(pg_sessions)
    with pg_sessions() as db:revision=edit(db,room,creator,'metadata',{'name':'New briefing'}).revision
    r=interleave(pg_sessions,lambda db:publish(db,room,creator,revision),lambda db:publish(db,room,creator,revision))
    assert r['first'][0]==r['second'][0]==200
    with pg_sessions() as db:
        assert db.scalar(select(func.count()).select_from(RoomRelease))==2
        assert db.get(Room,room).name=='New briefing' and not db.get(RoomDraft,room).dirty


@pytest.mark.parametrize('publish_first',[True,False])
def test_publication_and_grading_share_authoritative_lock(pg_sessions,publish_first):
    room,creator,learner,q=prepare(pg_sessions)
    with pg_sessions() as db:revision=edit(db,room,creator,'question_update',{'id':q,'points':75,'answer':'new-fixture-answer'}).revision
    def publish_call(db):return publish(db,room,creator,revision)
    def solve_call(db):return submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
    r=interleave(pg_sessions,publish_call if publish_first else solve_call,solve_call if publish_first else publish_call)
    assert r['first'][0]==200 and r['second'][0]==(404 if publish_first else 200)
    with pg_sessions() as db:
        old=db.get(ChallengeQuestion,q);assert not old.is_active and old.points==100
        awards=list(db.scalars(select(QuestionSolve)))
        assert len(awards)==(0 if publish_first else 1)
        if awards:assert awards[0].points_awarded==100
        new=db.scalar(select(ChallengeQuestion).where(ChallengeQuestion.room_id==room,ChallengeQuestion.is_active.is_(True)))
        assert new.points==75 and new.id!=q
        assert submit_answer(room,new.id,AnswerSubmission(answer='new-fixture-answer'),db.get(User,learner),db).points_awarded==75


def test_learner_launch_during_publication_selects_exact_new_image(pg_sessions):
    room,creator,learner,_=prepare(pg_sessions,practical=True)
    with pg_sessions() as db:
        revision=edit(db,room,creator,'metadata',{'name':'New image'}).revision
        draft=db.get(RoomDraft,room);draft.image_template_id=db.scalar(select(RoomTemplate.id).where(RoomTemplate.room_id==room,RoomTemplate.version==2));db.commit()
    background=BackgroundTasks()
    r=interleave(pg_sessions,lambda db:publish(db,room,creator,revision),lambda db:start_lab(room,LabStartRequest(),background,db.get(User,learner),db))
    assert r['first'][0]==r['second'][0]==200
    assert len(background.tasks)==1 and 'ami-new-fixture' in str(background.tasks[0].args)
    with pg_sessions() as db:assert db.scalar(select(func.count()).select_from(LabInstance))==1


def test_accepted_launch_keeps_original_ami_after_publication(pg_sessions):
    room,creator,learner,_=prepare(pg_sessions,practical=True)
    background=BackgroundTasks()
    with pg_sessions() as db:
        lab=start_lab(room,LabStartRequest(),background,db.get(User,learner),db)
        original=tuple(background.tasks[0].args)
        assert 'ami-old-fixture' in str(original)
        revision=edit(db,room,creator,'metadata',{'name':'Update'}).revision
        db.get(RoomDraft,room).image_template_id=db.scalar(select(RoomTemplate.id).where(RoomTemplate.room_id==room,RoomTemplate.version==2));db.commit()
        publish(db,room,creator,revision)
        assert background.tasks[0].args==original
        assert db.get(LabInstance,lab.id).status=='pending'


def test_migration_real_legacy_columns_and_reruns(pg_sessions):
    room,creator,learner,q=prepare(pg_sessions)
    with pg_sessions() as db:
        submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
        before=db.execute(text('SELECT question_id,learner_id,points_awarded,solved_at FROM question_solves')).all()
        db.execute(text('ALTER TABLE question_solves DROP COLUMN release_id'))
        db.execute(text('ALTER TABLE room_progress DROP COLUMN completed_release_id'))
        db.execute(text('DROP TABLE room_release_media'));db.execute(text('DROP TABLE room_media'));db.execute(text('DROP TABLE room_drafts'));db.execute(text('DROP TABLE room_releases'))
        for name in ['ssh_auth_mode','ssh_guest_username','source_ami_id']:db.execute(text('ALTER TABLE builder_environments DROP COLUMN '+name))
        db.commit()
        assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        assert db.execute(text('SELECT question_id,learner_id,points_awarded,solved_at FROM question_solves')).all()==before
        assert db.scalar(select(QuestionSolve.release_id)) is None
        assert db.scalar(select(RoomProgress.completed_at)) is not None


def test_full_cloud_migrator_adds_provenance_before_legacy_schema_verifier(pg_sessions):
    import runpy
    from pathlib import Path
    room,creator,learner,q=prepare(pg_sessions)
    with pg_sessions() as db:
        db.get(Room,room).category='linux';db.commit()
        submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
        before=db.execute(text('SELECT question_id,learner_id,points_awarded,solved_at FROM question_solves')).all()
        db.execute(text('ALTER TABLE question_solves DROP COLUMN release_id'))
        db.execute(text('ALTER TABLE room_progress DROP COLUMN completed_release_id'))
        db.execute(text('DROP TABLE room_release_media'));db.execute(text('DROP TABLE room_media'));db.execute(text('DROP TABLE room_drafts'));db.execute(text('DROP TABLE room_releases'))
        for name in ['source_ami_id','ssh_auth_mode','ssh_guest_username']:db.execute(text('ALTER TABLE builder_environments DROP COLUMN '+name))
        db.commit()
    script=Path(__file__).parent/'deploy/cloud/migrate.py'
    for _ in range(2):runpy.run_path(str(script),run_name='__main__')
    with pg_sessions() as db:
        assert db.execute(text('SELECT question_id,learner_id,points_awarded,solved_at FROM question_solves')).all()==before
        assert db.get(Room,room).status=='published'
        assert db.scalar(select(QuestionSolve.release_id)) is None
