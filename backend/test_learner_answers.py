"""Accepted inputs are private learner data, independently encrypted from answers."""
import base64
from unittest.mock import patch
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, inspect
from app.config import settings
from app.models import QuestionSolve, ChallengeQuestion
from app.learner_answers import encrypt_submission, decrypt_submission
from app.learner_answers_migration import migrate, REVISION
from test_challenges import env, add, publish, submit, progress


@pytest.fixture(autouse=True)
def test_key():
    with patch.object(settings, 'editable_answer_key', SecretStr(base64.urlsafe_b64encode(b'L'*32).decode())):
        yield


def reads(e, role='learner', room=None):
    return e.client.get(f'/api/rooms/{room or e.room}/accepted-answers', headers=e.headers[role] if role else {})


def test_exact_accepted_input_is_encrypted_and_survives_another_request(env):
    q=add(env,answer='SF{exact}').json()['id'];publish(env)
    value='  SF{exact}\t '
    result=submit(env,q,value);assert result.status_code==200 and result.json()['points_awarded']==100
    assert value not in result.text and 'ciphertext' not in result.text
    for _ in range(2):
        response=reads(env);assert response.status_code==200
        assert response.json()==[dict(question_id=q,answer=value,points_awarded=100,legacy=False)]
        assert 'no-store' in response.headers['cache-control']
    with env.maker() as db:
        solve=db.scalar(select(QuestionSolve));assert value not in solve.submission_ciphertext
        assert decrypt_submission(solve.submission_ciphertext,env.room,q,env.users['learner'])==value
    assert submit(env,q,'SF{exact}').json()['points_awarded']==0
    assert reads(env).json()[0]['answer']==value
    assert progress(env)['points_earned']==100


def test_incorrect_and_duplicate_attempts_never_capture_a_different_input(env):
    q=add(env).json()['id'];publish(env)
    assert not submit(env,q,'incorrect').json()['correct'];assert reads(env).json()==[]
    submit(env,q)
    assert not submit(env,q,'another wrong value').json()['correct']
    assert reads(env).json()[0]['answer']=='SF{offline-only}'
    with env.maker() as db:assert len(list(db.scalars(select(QuestionSolve))))==1


@pytest.mark.parametrize('role,expected',[('creator',200),('other',200),('admin',200),(None,401)])
def test_only_the_signed_in_solvers_own_input_is_retrieved(env,role,expected):
    q=add(env).json()['id'];publish(env);submit(env,q)
    response=reads(env,role)
    assert response.status_code in ([401,403] if role is None else [expected])
    if role:assert response.json()==[]
    assert 'no-store' in response.headers['cache-control']
    assert 'offline-only' not in response.text


def test_caller_cannot_select_someone_else_or_a_draft_room(env):
    q=add(env).json()['id'];assert reads(env).status_code==404
    publish(env);submit(env,q)
    response=env.client.get(f'/api/rooms/{env.room}/accepted-answers?learner_id={env.users["learner"]}',headers=env.headers['other'])
    assert response.status_code==200 and response.json()==[]


def test_two_solvers_retrieve_only_their_distinct_accepted_input(env):
    q=add(env,answer='flag').json()['id'];publish(env)
    assert submit(env,q,' flag').json()['points_awarded']==100
    response=env.client.post(f'/api/rooms/{env.room}/questions/{q}/submit',headers=env.headers['other'],json={'answer':'flag '})
    assert response.status_code==200 and response.json()['points_awarded']==100
    assert reads(env).json()[0]['answer']==' flag'
    assert reads(env,'other').json()[0]['answer']=='flag '


def test_publication_forks_keep_old_awards_without_showing_an_old_answer_for_a_new_question(env):
    from test_room_workspace import theory, release, edit, read
    theory(env);q=add(env,answer='oldflag').json()['id'];assert release(env).status_code==200
    submit(env,q,' oldflag ')
    assert edit(env,'question_update',{'id':q,'answer':'newflag','points':50}).status_code==200
    assert release(env).status_code==200
    new=read(env)['questions'][0]['id'];assert new!=q
    assert reads(env).json()==[] and progress(env)['points_earned']==100
    assert submit(env,new,'newflag').json()['points_awarded']==50
    assert reads(env).json()==[dict(question_id=new,answer='newflag',points_awarded=50,legacy=False)]
    with env.maker() as db:
        old=db.scalar(select(QuestionSolve).where(QuestionSolve.question_id==q))
        assert decrypt_submission(old.submission_ciphertext,env.room,q,env.users['learner'])==' oldflag '


def test_legacy_solves_are_never_backfilled_from_creator_or_retries(env):
    q=add(env).json()['id'];publish(env);submit(env,q)
    with env.maker() as db:
        solve=db.scalar(select(QuestionSolve));solve.submission_ciphertext=None;db.commit()
    before=progress(env)
    assert reads(env).json()==[dict(question_id=q,answer=None,points_awarded=100,legacy=True)]
    assert submit(env,q).json()['points_awarded']==0
    assert reads(env).json()[0]['legacy'];assert progress(env)==before


def test_no_answer_material_in_normal_room_progress_or_analytics(env):
    q=add(env).json()['id'];publish(env);submit(env,q)
    with env.maker() as db:ciphertext=db.scalar(select(QuestionSolve.submission_ciphertext))
    for path,role in [(f'/api/rooms/{env.room}','learner'),(f'/api/rooms/{env.room}/questions','learner'),(f'/api/rooms/{env.room}/progress','learner'),(f'/api/creator/analytics/rooms/{env.room}','creator'),('/api/public/rooms',None)]:
        response=env.client.get(path,headers=env.headers[role] if role else {})
        assert response.status_code==200
        assert 'SF{offline-only}' not in response.text and ciphertext not in response.text and 'submission_ciphertext' not in response.text


def test_wrong_key_corruption_or_swapped_context_fail_closed_but_credit_remains(env):
    q=add(env).json()['id'];publish(env);submit(env,q)
    before=progress(env)
    with env.maker() as db:
        solve=db.scalar(select(QuestionSolve));solve.submission_ciphertext=encrypt_submission('synthetic',env.room,q,env.users['other']);db.commit()
    response=reads(env);assert response.status_code==503 and 'no-store' in response.headers['cache-control']
    assert 'synthetic' not in response.text and progress(env)==before


def test_missing_key_rolls_back_the_entire_new_award(env):
    q=add(env).json()['id'];publish(env)
    with patch.object(settings,'editable_answer_key',SecretStr('')):assert submit(env,q).status_code==503
    assert progress(env)['points_earned']==0 and reads(env).json()==[]
    assert submit(env,q).json()['points_awarded']==100


@pytest.mark.parametrize('value,room,question,user', [('v2.bad',1,1,1),('v1.AAAA',1,1,1),(None,1,1,1)])
def test_envelope_validation(value,room,question,user):
    if value is None:assert decrypt_submission(value,room,question,user) is None
    else:
        with pytest.raises(Exception) as exc:decrypt_submission(value,room,question,user)
        assert exc.value.status_code==503


def test_nonce_randomness_and_room_question_user_binding():
    a=encrypt_submission('flag',1,2,3);b=encrypt_submission('flag',1,2,3);assert a!=b
    for args in [(2,2,3),(1,3,3),(1,2,4)]:
        with pytest.raises(Exception):decrypt_submission(a,*args)
    with patch.object(settings,'editable_answer_key',SecretStr(base64.urlsafe_b64encode(b'X'*32).decode())):
        with pytest.raises(Exception):decrypt_submission(a,1,2,3)


def test_additive_migration_preserves_existing_awards_and_is_idempotent(env):
    q=add(env).json()['id'];publish(env);submit(env,q)
    with env.maker() as db:
        fields='id,question_id,learner_id,points_awarded,solved_at,release_id'
        before=db.execute(text('SELECT '+fields+' FROM question_solves')).all()
        db.execute(text('ALTER TABLE question_solves DROP COLUMN submission_ciphertext'));db.commit()
        assert migrate(db.connection());db.commit();assert not migrate(db.connection());db.commit()
        assert db.execute(text('SELECT '+fields+' FROM question_solves')).all()==before
        assert db.scalar(text('SELECT count(*) FROM question_solves WHERE submission_ciphertext IS NOT NULL'))==0
        assert {v['name']:v for v in inspect(db.connection()).get_columns('question_solves')}['submission_ciphertext']['nullable']


def test_raw_whitespace_is_preserved_and_extreme_values_are_rejected_without_echo(env):
    q=add(env,answer='flag').json()['id'];publish(env)
    value=' '*3000+'flag';assert submit(env,q,value).status_code==200
    assert reads(env).json()[0]['answer']==value
    response=submit(env,q,' '*4096+'flag');assert response.status_code==422 and ' '*100 not in response.text
