"""Encrypted authoring copies, isolated SQLite APIs; no cloud/lifespan operations."""
import base64
import json
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, inspect

from app.config import settings
from app.editable_answers import decrypt_answer, encrypt_answer
from app.editable_answers_migration import migrate, REVISION
from app.models import ChallengeQuestion, RoomDraft, QuestionSolve, RoomProgress, SchemaRevision
from test_challenges import env, add, submit, progress
from test_room_workspace import read, edit, release, theory


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setattr(settings, 'editable_answer_key', SecretStr(base64.urlsafe_b64encode(b'X' * 32).decode()))


def answer(e, q, role='creator', draft=True, revision=None):
    return e.client.get(f'/api/rooms/{e.room}/workspace/questions/{q}/answer', headers=e.headers[role],
                        params={'draft': str(draft).lower(), **({'revision': revision} if revision is not None else {})})


def test_new_answers_are_recoverable_only_by_explicit_private_read(env):
    q = add(env, '  SF{Unicode_é_FLAG}  ').json()['id']
    assert answer(env, q).json() == {'answer': 'SF{Unicode_é_FLAG}', 'legacy': False}
    with env.maker() as db:
        row = db.get(ChallengeQuestion, q)
        assert 'SF{' not in row.answer_ciphertext
        assert decrypt_answer(row.answer_ciphertext, env.room, row.answer_salt) == 'SF{Unicode_é_FLAG}'
    for route in [f'/api/rooms/{env.room}', f'/api/rooms/{env.room}/questions', f'/api/rooms/{env.room}/workspace']:
        response = env.client.get(route, headers=env.headers['creator'])
        assert response.status_code == 200
        assert all(value not in response.text for value in ['SF{Unicode', 'answer_ciphertext', 'answer_salt', 'answer_digest'])
    result = answer(env, q)
    assert result.headers['cache-control'] == 'private, no-store' and result.headers['pragma'] == 'no-cache'


@pytest.mark.parametrize('role,status', [('creator',200), ('admin',200), ('learner',403), ('other',404)])
def test_owner_admin_authorization_and_no_cache_on_errors(env, role, status):
    q = add(env).json()['id']
    result = answer(env, q, role)
    assert result.status_code == status
    assert result.headers['cache-control'] == 'private, no-store'
    if status != 200: assert 'SF{offline-only}' not in result.text
    anonymous = env.client.get(f'/api/rooms/{env.room}/workspace/questions/{q}/answer')
    assert anonymous.status_code in (401,403) and 'offline-only' not in anonymous.text
    assert anonymous.headers['cache-control'] == 'private, no-store'


def test_draft_answer_and_revision_are_separate_from_published_answer(env):
    theory(env); q = add(env, 'oldflag').json()['id']; assert release(env).status_code == 200
    result = edit(env, 'question_update', {'id': q, 'answer': 'newflag'})
    assert result.status_code == 200
    revision = result.json()['revision']
    assert answer(env, q, revision=revision).json()['answer'] == 'newflag'
    assert answer(env, q, draft=False).json()['answer'] == 'oldflag'
    assert answer(env, q, revision=revision - 1).status_code == 409
    assert answer(env, 123456).status_code == 404
    assert 'newflag' not in str(read(env))
    with env.maker() as db:
        document = db.get(RoomDraft, env.room).document
        assert 'newflag' not in json.dumps(document)
        assert document['questions'][0]['answer_ciphertext'].startswith('v1.')
    assert submit(env, q, 'oldflag').json()['correct']
    assert not submit(env, q, 'newflag').json()['correct']


def test_draft_only_question_answer_can_be_opened(env):
    result = edit(env, 'question_create', {'question_text':'New question', 'answer':'draftflag','points':50})
    assert result.status_code == 200
    q = result.json()['questions'][0]['id']; assert q < 0
    assert answer(env, q).json()['answer'] == 'draftflag'
    assert answer(env, q, draft=False).status_code == 404


def test_legacy_blank_keeps_verifier_and_reentering_same_answer_keeps_credit(env):
    theory(env); q = add(env, 'legacyflag').json()['id']
    with env.maker() as db:
        row = db.get(ChallengeQuestion,q); row.answer_ciphertext = None
        original = (row.answer_salt,row.answer_digest); db.commit()
    assert answer(env,q).json() == {'answer':None,'legacy':True}
    assert edit(env,'question_update',{'id':q,'answer':''}).status_code == 422
    assert release(env).status_code == 200
    assert submit(env,q,'legacyflag').json()['points_awarded'] == 100
    earned = progress(env)
    assert edit(env,'question_update',{'id':q,'question_text':'Clearer wording'}).status_code == 200
    assert release(env).status_code == 200
    assert answer(env,q).json()['legacy']
    assert edit(env,'question_update',{'id':q,'answer':'legacyflag'}).status_code == 200
    assert release(env).status_code == 200
    assert answer(env,q).json() == {'answer':'legacyflag','legacy':False}
    with env.maker() as db:
        row = db.get(ChallengeQuestion,q)
        assert (row.answer_salt,row.answer_digest) == original and row.is_active
        assert len(list(db.scalars(select(QuestionSolve)))) == 1
    assert progress(env) == earned


def test_changed_encrypted_answer_forks_grading_without_rewriting_awards(env):
    theory(env); q = add(env,'firstflag').json()['id']; assert release(env).status_code == 200
    assert submit(env,q,'firstflag').json()['points_awarded'] == 100
    assert edit(env,'question_update',{'id':q,'answer':'newflag','points':50}).status_code == 200
    assert release(env).status_code == 200
    new = read(env)['questions'][0]['id']; assert new != q
    assert answer(env,new).json()['answer'] == 'newflag'
    with env.maker() as db:
        old = db.get(ChallengeQuestion,q)
        assert not old.is_active and decrypt_answer(old.answer_ciphertext,env.room,old.answer_salt) == 'firstflag'
        award = db.scalar(select(QuestionSolve)); assert award.question_id == q and award.points_awarded == 100
    assert submit(env,new,'newflag').json()['points_awarded'] == 50
    assert progress(env)['points_earned'] == 150


def test_ciphertexts_are_randomized_bound_and_fail_closed():
    first = encrypt_answer('flag',1,'salt'); second = encrypt_answer('flag',1,'salt')
    assert first != second
    for value,room,salt in [(first,2,'salt'),(first,1,'other'),('v9.bad',1,'salt'),(first[:-4]+'AAAA',1,'salt')]:
        with pytest.raises(Exception) as exc: decrypt_answer(value,room,salt)
        assert exc.value.status_code == 503 and 'flag' not in str(exc.value.detail)
    with patch.object(settings,'editable_answer_key',SecretStr(base64.urlsafe_b64encode(b'Y'*32).decode())):
        with pytest.raises(Exception) as exc: decrypt_answer(first,1,'salt')
        assert exc.value.status_code == 503


def test_unavailable_key_preserves_rows_and_unreadable_is_not_legacy(env):
    q = add(env,'realflag').json()['id']; before = read(env)
    with patch.object(settings,'editable_answer_key',SecretStr('')):
        assert answer(env,q).status_code == 503
        result = edit(env,'question_update',{'id':q,'answer':'newflag'})
        assert result.status_code == 503 and 'newflag' not in result.text
    assert read(env) == before and answer(env,q).json()['answer'] == 'realflag'
    with env.maker() as db:
        db.get(ChallengeQuestion,q).answer_ciphertext = 'v1.corrupt'; db.commit()
    result = answer(env,q)
    assert result.status_code == 503 and 'legacy' not in result.text


def test_learner_public_and_analytics_payloads_never_contain_answer_material(env):
    theory(env); q=add(env,'confidentialflag').json()['id']; assert release(env).status_code==200
    assert edit(env,'question_update',{'id':q,'answer':'confidentialdraft'}).status_code==200
    for path,role in [(f'/api/rooms/{env.room}','learner'),(f'/api/rooms/{env.room}/questions','learner'),('/api/public/rooms','learner'),(f'/api/public/rooms/{env.room}','learner'),('/api/creator/analytics','creator'),('/api/admin/content/rooms','admin')]:
        result=env.client.get(path,headers=env.headers[role]);assert result.status_code==200
        assert all(v not in result.text for v in ['confidentialflag','confidentialdraft','answer_ciphertext','answer_digest','answer_salt'])


def test_nullable_migration_preserves_legacy_question_fields(env):
    q=add(env).json()['id']
    with env.maker() as db:
        before=db.execute(text('SELECT id,room_id,question_text,answer_salt,answer_digest,points,is_active FROM challenge_questions')).all()
        db.execute(text('ALTER TABLE challenge_questions DROP COLUMN answer_ciphertext'));db.commit()
        assert migrate(db.connection()) is True;db.commit()
        assert migrate(db.connection()) is False;db.commit()
        assert db.execute(text('SELECT id,room_id,question_text,answer_salt,answer_digest,points,is_active FROM challenge_questions')).all()==before
        assert db.execute(text('SELECT answer_ciphertext FROM challenge_questions WHERE id=:q'),{'q':q}).scalar_one() is None
        assert {c['name']:c for c in inspect(db.connection()).get_columns('challenge_questions')}['answer_ciphertext']['nullable']


def test_migration_refuses_incompatible_existing_storage(env):
    with env.maker() as db:
        db.execute(text('ALTER TABLE challenge_questions DROP COLUMN answer_ciphertext'))
        db.execute(text('ALTER TABLE challenge_questions ADD COLUMN answer_ciphertext INTEGER'));db.commit()
        with pytest.raises(RuntimeError,match='Incompatible'): migrate(db.connection())
        db.rollback()
        assert db.scalar(select(SchemaRevision.revision).where(SchemaRevision.revision==REVISION)) is None


def test_ciphertext_that_does_not_match_the_verifier_is_never_returned(env):
    q=add(env,'sensitiveflag').json()['id']
    with env.maker() as db:
        db.get(ChallengeQuestion,q).answer_digest='0'*64;db.commit()
    result=answer(env,q)
    assert result.status_code==503 and 'sensitiveflag' not in result.text
