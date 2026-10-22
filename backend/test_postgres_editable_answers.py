"""Activated only by the owned disposable PostgreSQL runner."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned PostgreSQL concurrency runner', allow_module_level=True)

from sqlalchemy import text
from app.models import User, ChallengeQuestion, QuestionSolve
from app.editable_answers import decrypt_answer
from app.editable_answers_migration import migrate
from app.room_workspace import editable_answer
from app.challenges import submit_answer
from app.schemas import AnswerSubmission
from test_postgres_lab_concurrency import pg_sessions
from test_postgres_workspace import prepare, edit, publish


def test_encrypted_answer_publication_preserves_awarded_credit(pg_sessions):
    room,creator,learner,q=prepare(pg_sessions)
    with pg_sessions() as db:
        submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
        revision=edit(db,room,creator,'question_update',{'id':q,'answer':'new PostgreSQL flag'}).revision
        assert editable_answer(room,q,True,revision,db.get(User,creator),db).answer=='new PostgreSQL flag'
        publish(db,room,creator,revision)
        old=db.get(ChallengeQuestion,q);assert not old.is_active
        assert db.query(QuestionSolve).one().points_awarded==100
        new=db.query(ChallengeQuestion).filter(ChallengeQuestion.room_id==room,ChallengeQuestion.is_active.is_(True)).one()
        assert decrypt_answer(new.answer_ciphertext,room,new.answer_salt)=='new PostgreSQL flag'
        assert submit_answer(room,new.id,AnswerSubmission(answer='new PostgreSQL flag'),db.get(User,learner),db).points_awarded==100


def test_additive_answer_migration_retains_all_old_fields(pg_sessions):
    room,_,_,_=prepare(pg_sessions)
    with pg_sessions() as db:
        before=db.execute(text('SELECT id,answer_salt,answer_digest,points,is_active FROM challenge_questions')).all()
        db.execute(text('ALTER TABLE challenge_questions DROP COLUMN answer_ciphertext'));db.commit()
        assert migrate(db.connection());db.commit()
        assert not migrate(db.connection());db.commit()
        assert db.execute(text('SELECT id,answer_salt,answer_digest,points,is_active FROM challenge_questions')).all()==before
        assert db.execute(text('SELECT count(*) FROM challenge_questions WHERE answer_ciphertext IS NOT NULL')).scalar_one()==0
