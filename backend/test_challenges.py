"""Offline learning API/security tests; isolated SQLite per case, no lifespan operations."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base, get_db
from app.main import app
from app.models import ChallengeQuestion, LabInstance, QuestionSolve, Room, RoomProgress, User, RoomTemplate, MachineTemplate
from app.security import create_access_token
from app.challenge_migration import migrate, TABLES

@pytest.fixture
def env():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    @event.listens_for(engine,'connect')
    def foreign_keys(connection,_): connection.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine);maker=sessionmaker(engine,autoflush=False)
    with maker() as db:
        users=[User(username=r,email=r+'@example.com',role='creator' if r=='other' else r,password_hash='not-used') for r in ['creator','other','learner','admin']]
        db.add_all(users);db.flush()
        room=Room(creator_id=users[0].id,name='Learning test',category='Web',difficulty='easy',status='draft')
        other=Room(creator_id=users[1].id,name='Other',category='Web',difficulty='easy',status='published')
        db.add_all([room,other]);db.commit()
        result=SimpleNamespace(maker=maker,room=room.id,other_room=other.id,users={u.username:u.id for u in users},headers={u.username:{'Authorization':'Bearer '+create_access_token(u.id,u.role)} for u in users})
    def database():
        with maker() as db: yield db
    previous=dict(app.dependency_overrides);app.dependency_overrides[get_db]=database
    result.client=TestClient(app)
    yield result
    result.client.close();app.dependency_overrides.clear();app.dependency_overrides.update(previous);engine.dispose()

def add(e,answer='SF{offline-only}',**fields):
    return e.client.post(f'/api/rooms/{e.room}/questions',headers=e.headers['creator'],json={'question_text':'Find the flag','answer':answer,'points':100,**fields})
def publish(e):
    # Practical publication must pin a ready image, even in grading-only fixtures.
    with e.maker() as db:
        version=db.scalar(select(RoomTemplate).where(RoomTemplate.room_id==e.room))
        if version is None:
            version=RoomTemplate(room_id=e.room,created_by=e.users['creator'],version=1);db.add(version);db.flush()
            db.add(MachineTemplate(room_template_id=version.id,ami_id='ami-isolated-fixture',status='available'));db.commit()
        vid=version.id
    assert e.client.post(f'/api/rooms/{e.room}/templates/{vid}/publish',headers=e.headers['creator']).status_code==200
def submit(e,q,answer='SF{offline-only}'):
    return e.client.post(f'/api/rooms/{e.room}/questions/{q}/submit',headers=e.headers['learner'],json={'answer':answer})
def progress(e):
    response=e.client.get(f'/api/rooms/{e.room}/progress',headers=e.headers['learner']);assert response.status_code==200
    return response.json()

def test_author_crud_order_preview_and_no_plaintext(env):
    first=add(env);assert first.status_code==201
    second=add(env,question_text='Second task',points=50,display_order=1);assert second.status_code==201
    q,q2=first.json()['id'],second.json()['id']
    response=env.client.get(f'/api/rooms/{env.room}/questions',headers=env.headers['creator']).json()
    assert [r['id'] for r in response['questions']]==[q2,q]
    assert not any(s in str(response) for s in ['SF{offline-only}','answer_digest','answer_salt'])
    with env.maker() as db:
        stored=db.get(ChallengeQuestion,q);assert len(stored.answer_digest)==64 and len(stored.answer_salt)==32
        assert 'offline-only' not in stored.answer_digest
    updated=env.client.patch(f'/api/rooms/{env.room}/questions/{q}',headers=env.headers['creator'],json={'question_text':'Updated text','hint':'Look closely','answer':'NewFlag','points':40})
    assert updated.status_code==200 and updated.json()['hint']=='Look closely' and 'NewFlag' not in updated.text
    reordered=env.client.put(f'/api/rooms/{env.room}/questions/order',headers=env.headers['creator'],json={'question_ids':[q,q2]})
    assert reordered.status_code==200 and [r['display_order'] for r in reordered.json()['questions']]==[1,2]
    assert env.client.delete(f'/api/rooms/{env.room}/questions/{q}',headers=env.headers['creator']).status_code==204
    assert env.client.get(f'/api/rooms/{env.room}/questions',headers=env.headers['creator']).json()['questions'][0]['display_order']==1
    with env.maker() as db: assert not db.get(ChallengeQuestion,q).is_active

@pytest.mark.parametrize('role,status',[('other',404),('learner',403)])
def test_only_owner_or_admin_can_edit(env,role,status):
    q=add(env).json()['id'];url=f'/api/rooms/{env.room}/questions'
    assert env.client.post(url,headers=env.headers[role],json={'question_text':'x','answer':'x'}).status_code==status
    assert env.client.patch(url+f'/{q}',headers=env.headers[role],json={'hint':'x'}).status_code==status
    assert env.client.delete(url+f'/{q}',headers=env.headers[role]).status_code==status
    assert env.client.put(url+'/order',headers=env.headers[role],json={'question_ids':[q]}).status_code==status

def test_admin_authoring_and_cross_room_question_denial(env):
    q=add(env).json()['id']
    assert env.client.patch(f'/api/rooms/{env.room}/questions/{q}',headers=env.headers['admin'],json={'hint':'Admin clarification'}).status_code==404
    assert env.client.patch(f'/api/rooms/{env.other_room}/questions/{q}',headers=env.headers['admin'],json={'hint':'wrong room'}).status_code==404

@pytest.mark.parametrize('change',[{'question_text':'  '},{'answer':'  '},{'answer':'X'*513},{'question_text':'X'*1001},{'hint':'X'*2001},{'points':0},{'points':1001},{'points':True},{'display_order':0},{'display_order':2}])
def test_validation_does_not_echo_flag(env,change):
    response=add(env,**change)
    assert response.status_code==422 and 'SF{offline-only}' not in response.text and 'XXXX' not in response.text
    with env.maker() as db: assert db.scalar(select(ChallengeQuestion.id)) is None

def test_invalid_order_is_atomic(env):
    q=add(env).json()['id'];q2=add(env,question_text='two').json()['id']
    for order in ([q,q],[q2],[q,999]):
        assert env.client.put(f'/api/rooms/{env.room}/questions/order',headers=env.headers['creator'],json={'question_ids':order}).status_code==422
    assert [q['id'] for q in env.client.get(f'/api/rooms/{env.room}/questions',headers=env.headers['creator']).json()['questions']]==[q,q2]

def test_published_access_and_role_restrictions(env):
    q=add(env).json()['id']
    for endpoint in ['questions','progress']:
        assert env.client.get(f'/api/rooms/{env.room}/{endpoint}',headers=env.headers['learner']).status_code==404
    assert submit(env,q).status_code==404
    publish(env)
    assert env.client.get(f'/api/rooms/{env.room}/questions',headers=env.headers['learner']).status_code==200
    for role in ['creator','admin']:
        assert env.client.post(f'/api/rooms/{env.room}/questions/{q}/submit',headers=env.headers[role],json={'answer':'x'}).status_code==(403 if role=='creator' else 200)
        assert env.client.get(f'/api/rooms/{env.room}/progress',headers=env.headers[role]).status_code==200
    assert env.client.post(f'/api/rooms/{env.other_room}/questions/{q}/submit',headers=env.headers['learner'],json={'answer':'x'}).status_code==404

def test_correct_incorrect_normalization_and_once_only_scoring(env):
    q=add(env,answer='SF{Case Sensitive}',points=125).json()['id'];publish(env)
    for answer in ['wrong','sf{Case Sensitive}','SF{Case  Sensitive}']:
        r=submit(env,q,answer);assert r.status_code==200 and not r.json()['correct'] and r.json()['points_awarded']==0
    r=submit(env,q,' \tSF{Case Sensitive}\n').json()
    assert r['correct'] and r['points_awarded']==125 and r['progress']['completed']
    stamp=r['progress']['completed_at']
    for _ in range(3):
        repeated=submit(env,q,'SF{Case Sensitive}').json()
        assert repeated['already_solved'] and repeated['points_awarded']==0
        assert repeated['progress']['points_earned']==125 and repeated['progress']['completed_at']==stamp
    assert not submit(env,q,'wrong').json()['correct']
    with env.maker() as db: assert len(db.scalars(select(QuestionSolve)).all())==1
    assert 'Case Sensitive' not in env.client.get(f'/api/rooms/{env.room}/questions',headers=env.headers['learner']).text

def test_multiple_questions_completion_and_refresh_persistence(env):
    q=add(env,points=50).json()['id'];q2=add(env,answer='second',question_text='Second question',points=75).json()['id'];publish(env)
    assert progress(env)['questions_solved']==0
    assert not submit(env,q).json()['progress']['completed']
    final=submit(env,q2,'second').json()['progress']
    assert final['points_earned']==125 and final['completed'] and final['questions_solved']==2
    assert progress(env)==final

def test_existing_room_without_questions_is_not_auto_completed(env):
    publish(env);p=progress(env)
    assert p['questions_total']==0 and p['points_earned']==0 and not p['completed'] and p['completed_at'] is None
    with env.maker() as db: assert db.scalar(select(RoomProgress.id)) is None

def test_rate_limit_persisted_per_learner_room_and_recovers(env):
    q=add(env).json()['id'];publish(env)
    for _ in range(20): assert submit(env,q,'wrong').status_code==200
    rejected=submit(env,q);assert rejected.status_code==429 and int(rejected.headers['Retry-After'])>0
    with env.maker() as db:
        p=db.scalar(select(RoomProgress));assert p.attempt_count==20
        p.attempt_window_started_at=datetime.utcnow()-timedelta(seconds=61);db.commit()
    assert submit(env,q).json()['points_awarded']==100

def test_edits_after_publication_preserve_earned_credit(env):
    from test_room_workspace import edit, release, read
    q=add(env).json()['id'];publish(env)
    assert env.client.patch(f'/api/rooms/{env.room}/questions/{q}',headers=env.headers['creator'],json={'answer':'replacement','points':25}).status_code==409
    assert edit(env,'question_update',{'id':q,'answer':'replacement','points':25}).status_code==200
    assert release(env).status_code==200
    q=read(env)['questions'][0]['id']
    assert submit(env,q,'replacement').json()['points_awarded']==25
    before=progress(env)
    assert env.client.get(f'/api/rooms/{env.room}/questions',headers=env.headers['creator']).json()['authoring_locked']
    for payload in [{'answer':'another'},{'points':75}]:
        assert env.client.patch(f'/api/rooms/{env.room}/questions/{q}',headers=env.headers['creator'],json=payload).status_code==409
    assert add(env).status_code==409
    assert env.client.delete(f'/api/rooms/{env.room}/questions/{q}',headers=env.headers['creator']).status_code==409
    assert edit(env,'question_update',{'id':q,'question_text':'Clarified task','hint':'A clearer hint'}).status_code==200
    assert edit(env,'question_order',{'ids':[q]}).status_code==200
    assert progress(env)==before
    assert release(env).status_code==200 and progress(env)==before
    assert env.client.post(f'/api/rooms/{env.room}/unpublish',headers=env.headers['creator']).status_code==200
    assert env.client.delete(f'/api/rooms/{env.room}',headers=env.headers['creator']).status_code==409
    assert release(env).status_code==200 and progress(env)==before


def test_lab_stop_does_not_erase_progress(env,monkeypatch):
    q=add(env).json()['id'];publish(env);submit(env,q);before=progress(env)
    with env.maker() as db:
        lab=LabInstance(room_id=env.room,learner_id=env.users['learner'],status='running',expires_at=datetime.utcnow()+timedelta(hours=1),terraform_state_path='offline-only')
        db.add(lab);db.commit()
    def destroy(_self,lab,db):
        lab.status='stopped';db.commit()
    monkeypatch.setattr('app.lab_manager.LabManager.stop_lab_instance',destroy)
    monkeypatch.setattr('app.labs.SessionLocal',env.maker)
    assert env.client.post(f'/api/rooms/{env.room}/lab/stop',headers=env.headers['learner']).status_code==200
    assert progress(env)==before

def test_database_failure_returns_sanitized_503(env):
    from sqlalchemy.exc import OperationalError
    with patch('app.challenges.active_questions',side_effect=OperationalError('private statement',{},Exception('private diagnostic'))):
        r=env.client.get(f'/api/rooms/{env.room}/questions',headers=env.headers['creator'])
    assert r.status_code==503 and 'private' not in r.text

def test_additive_migration_keeps_legacy_rooms_and_is_idempotent():
    engine=create_engine('sqlite://');old=[t for t in Base.metadata.sorted_tables if t not in TABLES]
    Base.metadata.create_all(engine,tables=old)
    with engine.begin() as db:
        db.execute(User.__table__.insert().values(id=1,username='legacy',email='legacy@example.com',password_hash='existing',role='creator'))
        db.execute(Room.__table__.insert().values(id=1089,creator_id=1,name='Unchanged',category='Web',difficulty='easy',status='published'))
        assert migrate(db) and not migrate(db)
        assert db.scalar(select(Room.name))=='Unchanged' and db.scalar(select(ChallengeQuestion.id)) is None
        assert set(inspect(db).get_table_names()).issuperset(t.name for t in TABLES)
    engine.dispose()

def test_migration_refuses_incompatible_tables_without_resetting():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine,tables=[User.__table__,Room.__table__])
    with engine.begin() as db:
        db.execute(text('CREATE TABLE challenge_questions (unrelated integer)'))
        with pytest.raises(RuntimeError):migrate(db)
        assert [c['name'] for c in inspect(db).get_columns('challenge_questions')]==['unrelated']
    engine.dispose()


def test_migration_refuses_existing_schema_without_award_uniqueness():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(text('DROP INDEX uq_challenge_question_order'))
        with pytest.raises(RuntimeError):migrate(db)
        assert inspect(db).has_table('rooms')
    engine.dispose()

@pytest.mark.parametrize('answer',[' ', 'X'*513, None, ['invalid']])
def test_invalid_submission_does_not_echo_input_or_create_progress(env,answer):
    q=add(env).json()['id'];publish(env)
    response=submit(env,q,answer)
    assert response.status_code==422 and 'XXXX' not in response.text and '[\"invalid\"]' not in response.text
    with env.maker() as db:assert db.scalar(select(RoomProgress.id)) is None


def test_failed_award_commit_rolls_back_and_retry_awards_once(env):
    from sqlalchemy.exc import OperationalError
    q=add(env).json()['id'];publish(env)
    with patch('sqlalchemy.orm.Session.commit',side_effect=OperationalError('private statement',{},Exception('private diagnostic'))):
        response=submit(env,q)
    assert response.status_code==503 and 'private' not in response.text
    with env.maker() as db:
        assert db.scalar(select(QuestionSolve.id)) is None
        assert db.scalar(select(RoomProgress.id)) is None
    assert submit(env,q).json()['points_awarded']==100
    assert submit(env,q).json()['points_awarded']==0


def test_room_question_limit_cannot_be_exceeded(env):
    with env.maker() as db:
        db.add_all([ChallengeQuestion(room_id=env.room,question_text=f'Task {i}',points=1,
            display_order=i,answer_salt='a'*32,answer_digest='b'*64) for i in range(1,101)])
        db.commit()
    assert add(env).status_code==409
    with env.maker() as db:assert len(db.scalars(select(ChallengeQuestion)).all())==100
