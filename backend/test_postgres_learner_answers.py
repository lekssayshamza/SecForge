"""Real PostgreSQL verification; only the owned disposable runner enables this."""
import pytest
from test_support import postgres_harness as harness
if harness.ACTIVE_CLUSTER is None or not harness.ACTIVE_CLUSTER.started:
    pytest.skip('Requires the owned PostgreSQL runner',allow_module_level=True)
from sqlalchemy import select,text,inspect
from app import challenges
from app.models import User,QuestionSolve
from app.schemas import AnswerSubmission
from app.learner_answers import decrypt_submission
from app.learner_answers_migration import migrate
from test_postgres_lab_concurrency import pg_sessions
from test_postgres_challenges import seed,interleave


def test_duplicate_concurrent_submissions_keep_one_award_and_the_first_accepted_input(pg_sessions):
    room,_,learner,_,q,_=seed(pg_sessions)
    def solve(value):
        def call(db):return challenges.submit_answer(room,q,AnswerSubmission(answer=value),db.get(User,learner),db)
        return call
    result=interleave(pg_sessions,solve(' offline flag'),solve('offline flag '))
    assert sorted(value[1].points_awarded for value in result.values())==[0,100]
    with pg_sessions() as db:
        records=list(db.scalars(select(QuestionSolve)));assert len(records)==1
        assert decrypt_submission(records[0].submission_ciphertext,room,q,learner)==' offline flag'
        assert challenges.progress_response(room,learner,db).points_earned==100


def test_accepted_submission_migration_is_nullable_additive_and_repeatable(pg_sessions):
    room,_,learner,_,q,_=seed(pg_sessions)
    with pg_sessions() as db:
        challenges.submit_answer(room,q,AnswerSubmission(answer='offline flag'),db.get(User,learner),db)
        fields='id,question_id,learner_id,points_awarded,solved_at,release_id'
        before=db.execute(text('SELECT '+fields+' FROM question_solves')).all()
        db.execute(text('ALTER TABLE question_solves DROP COLUMN submission_ciphertext'));db.commit()
        assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        assert db.execute(text('SELECT '+fields+' FROM question_solves')).all()==before
        assert db.scalar(text('SELECT count(*) FROM question_solves WHERE submission_ciphertext IS NOT NULL'))==0
        assert {v['name']:v for v in inspect(db.connection()).get_columns('question_solves')}['submission_ciphertext']['nullable']
