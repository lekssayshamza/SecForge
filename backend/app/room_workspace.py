"""Owner drafts and atomic releases. Live rows remain the learner read model.

Room locks serialize grading, launch selection, capture registration and publishing.
No AWS operation occurs in this module. Historical awards are never rewritten.
"""
from copy import deepcopy
from datetime import datetime
from typing import Literal
import secrets
import hmac

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.challenges import ConfidentialValidationRoute, answer_digest, transaction
from app.database import get_db
from app.models import (Room, RoomDraft, RoomRelease, RoomTask, ChallengeQuestion,
                        RoomTemplate, MachineTemplate, User)
from app.permissions import has_creator_access
from app.room_tasks import TaskWrite
from app.schemas import QuestionCreate, QuestionUpdate, QuestionResponse, RoomUpdate, RoomResponse
from app.publication import validate_room
from app.editable_answers import encrypt_answer, decrypt_answer

def private_response(response: Response):
    response.headers['Cache-Control'] = 'private, no-store'


router = APIRouter(prefix='/rooms', tags=['Creator workspace'], route_class=ConfidentialValidationRoute, dependencies=[Depends(private_response)])
META = ('name', 'description', 'category', 'difficulty', 'objectives', 'prerequisites', 'exposed_ports')
QUESTION = ('id', 'task_id', 'question_text', 'hint', 'points', 'display_order', 'answer_salt', 'answer_digest', 'answer_ciphertext')
TASK = ('id', 'title', 'instructions', 'display_order')


class DraftQuestion(QuestionResponse):
    has_saved_answer: bool = True


class DraftTask(BaseModel):
    id: int
    title: str
    instructions: str
    display_order: int


class WorkspaceResponse(BaseModel):
    room: RoomResponse
    revision: int
    dirty: bool
    questions: list[DraftQuestion]
    tasks: list[DraftTask]
    image_status: str | None
    image_template_id: int | None
    image_is_published: bool


class WorkspaceWrite(BaseModel):
    revision: int = Field(ge=0, strict=True)
    action: Literal['metadata', 'question_create', 'question_update', 'question_remove',
                    'question_order', 'task_create', 'task_update', 'task_remove', 'task_order']
    data: dict
    model_config = {'extra': 'forbid'}


class PublishWrite(BaseModel):
    revision: int = Field(ge=0, strict=True)
    model_config = {'extra': 'forbid'}


def owned(room_id, user, db):
    if not has_creator_access(user):
        raise HTTPException(403, 'Creator access required')
    room = db.scalar(select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True))
    if room is None or room.creator_id != user.id:
        raise HTTPException(404, 'Room not found')
    return room


def document(room, db):
    return {'metadata': {**{k: getattr(room, k) for k in META}, 'categories': room.categories},
            'tasks': [{k: getattr(t, k) for k in TASK} for t in db.scalars(select(RoomTask).where(RoomTask.room_id == room.id).order_by(RoomTask.display_order))],
            'questions': [{k: getattr(q, k) for k in QUESTION} for q in db.scalars(select(ChallengeQuestion).where(ChallengeQuestion.room_id == room.id, ChallengeQuestion.is_active.is_(True)).order_by(ChallengeQuestion.display_order))]}


def image_selection(room, db):
    if room.room_type == 'THEORY':
        return None
    # A published room's working copy starts from its published image. Initial
    # drafts select the latest capture even when failed; never fall back silently.
    query = select(RoomTemplate).where(RoomTemplate.room_id == room.id)
    if room.status == 'published':
        query = query.where(RoomTemplate.is_published.is_(True))
    template = db.scalar(query.order_by(RoomTemplate.version.desc()).limit(1))
    return template.id if template else None


def working(room, db):
    draft = db.get(RoomDraft, room.id)
    if draft:
        return draft
    return RoomDraft(room_id=room.id, document=document(room, db), revision=0, dirty=False,
                     image_template_id=image_selection(room, db))


def response(room, draft, db):
    metadata = RoomResponse.model_validate(room).model_dump()
    metadata.update(draft.document['metadata'])
    metadata['categories'] = draft.document['metadata'].get('categories', [draft.document['metadata']['category']])
    machine = db.scalar(select(MachineTemplate).where(MachineTemplate.room_template_id == draft.image_template_id, MachineTemplate.role == 'primary').order_by(MachineTemplate.id).limit(1)) if draft.image_template_id else None
    template = db.get(RoomTemplate, draft.image_template_id) if draft.image_template_id else None
    return WorkspaceResponse(room=metadata, revision=draft.revision, dirty=draft.dirty,
        tasks=[DraftTask(**t) for t in draft.document['tasks']],
        questions=[DraftQuestion(**{k: q[k] for k in QuestionResponse.model_fields if k in q}) for q in draft.document['questions']],
        image_status=machine.status if machine else None,
        image_template_id=draft.image_template_id,
        image_is_published=bool(template and template.is_published))


def legacy_write_guard(room, db):
    """Old API callers cannot mutate the learner read model behind a draft."""
    draft = db.get(RoomDraft, room.id)
    if room.status == 'published' or draft is not None:
        raise HTTPException(409, 'Use the creator workspace to save unpublished changes, then publish them.')


def register_capture(room, template_id, db):
    draft = working(room, db)
    draft.image_template_id = template_id
    draft.revision += 1
    draft.dirty = True
    draft.updated_at = datetime.utcnow()
    db.add(draft)


@router.get('/{room_id}/workspace', response_model=WorkspaceResponse)
def get_workspace(room_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    room = owned(room_id, user, db)
    return response(room, working(room, db), db)


class EditableAnswerResponse(BaseModel):
    answer: str | None = Field(repr=False)
    legacy: bool


@router.get('/{room_id}/workspace/questions/{question_id}/answer', response_model=EditableAnswerResponse)
def editable_answer(room_id: int, question_id: int, draft: bool = True, revision: int | None = Query(default=None, ge=0),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not has_creator_access(user):
        raise HTTPException(403, 'Creator access required')
    room = db.get(Room, room_id)
    if room is None or (room.creator_id != user.id and user.role != 'admin'):
        raise HTTPException(404, 'Room not found')
    if draft:
        current = working(room, db)
        if revision is not None and current.revision != revision:
            raise HTTPException(409, 'The draft changed. Reopen the question editor.')
        question = select_item(current.document['questions'], question_id)
    else:
        from app.challenges import selected_question
        q = selected_question(room_id, question_id, db)
        question = {k: getattr(q, k) for k in QUESTION}
    value = decrypt_answer(question.get('answer_ciphertext'), room_id, question['answer_salt'])
    if value is not None and not hmac.compare_digest(answer_digest(value, question['answer_salt']), question['answer_digest']):
        raise HTTPException(503, 'The saved answer could not be verified. Please contact an administrator.')
    return EditableAnswerResponse(answer=value, legacy=value is None)


def select_item(items, item_id):
    if type(item_id) is not int:
        raise HTTPException(422, 'Select an item from this draft')
    item = next((i for i in items if i['id'] == item_id), None)
    if item is None:
        raise HTTPException(404, 'Draft item not found')
    return item


def normalize_order(items):
    for index, item in enumerate(items):
        item['display_order'] = index + 1


def validated(schema, payload):
    # All inner payloads are strict; never silently accept privilege or hash fields.
    if set(payload) - set(schema.model_fields):
        raise HTTPException(422, 'Unsupported draft fields')
    try:
        return schema.model_validate(payload)
    except ValidationError:
        raise HTTPException(422, 'Invalid draft content') from None


def apply_edit(doc, action, payload, room_id):
    data = deepcopy(payload)
    if action == 'metadata':
        from app.rooms import validate_name, validate_description, validate_difficulty
        values = validated(RoomUpdate, data).model_dump(exclude_unset=True)
        if any(v is None for k, v in values.items() if k != 'description'):
            raise HTTPException(422, 'Metadata cannot be empty')
        if 'name' in values: validate_name(values['name'])
        if 'description' in values: validate_description(values['description'])
        if 'difficulty' in values: validate_difficulty(values['difficulty'])
        if 'categories' in values:
            if 'category' in values and values['category'] != values['categories'][0]:raise HTTPException(422, 'Primary category must be the first selected category')
            values['category'] = values['categories'][0]
        elif 'category' in values:values['categories'] = [values['category']]
        doc['metadata'].update(values)
        return
    kind, verb = action.split('_')
    items = doc['questions' if kind == 'question' else 'tasks']
    if verb == 'order':
        ids = data.get('ids')
        if set(data) != {'ids'} or not isinstance(ids, list) or any(type(i) is not int for i in ids) or len(ids) != len(set(ids)) or set(ids) != {i['id'] for i in items}:
            raise HTTPException(422, 'Provide every draft item exactly once')
        items[:] = [select_item(items, i) for i in ids]
    elif verb == 'remove':
        if set(data) != {'id'}: raise HTTPException(422, 'Invalid removal')
        item = select_item(items, data['id'])
        items.remove(item)
        if kind == 'task':
            for q in doc['questions']:
                if q['task_id'] == item['id']: q['task_id'] = None
    else:
        item = select_item(items, data.pop('id', None)) if verb == 'update' else None
        if kind == 'question':
            task_id = data.pop('task_id', item['task_id'] if item else None)
            if task_id is not None: select_item(doc['tasks'], task_id)
            if 'display_order' in data: raise HTTPException(422, 'Use the ordering action')
            value = validated(QuestionUpdate if item else QuestionCreate, data)
            fields = value.model_dump(exclude_unset=bool(item), exclude={'answer', 'task_id', 'display_order'})
            fields['task_id'] = task_id
            if value.answer is not None:
                answer = value.answer.get_secret_value()
                same = item is not None and hmac.compare_digest(answer_digest(answer, item['answer_salt']), item['answer_digest'])
                salt = item['answer_salt'] if same else secrets.token_hex(16)
                fields['answer_salt'] = salt
                fields['answer_digest'] = answer_digest(answer, salt)
                fields['answer_ciphertext'] = encrypt_answer(answer, room_id, salt)
        else:
            fields = validated(TaskWrite, data).model_dump()
        if item:
            item.update(fields)
        else:
            if len(items) >= (100 if kind == 'question' else 30): raise HTTPException(409, 'Draft item limit reached')
            # Negative IDs are draft-local and cannot collide with live IDs.
            next_id = min([i['id'] for i in doc['tasks'] + doc['questions']] + [0]) - 1
            items.append({'id': next_id, **fields})
    normalize_order(items)


@router.patch('/{room_id}/workspace', response_model=WorkspaceResponse)
def edit_workspace(room_id: int, data: WorkspaceWrite, user: User = Depends(get_current_user), db: Session = Depends(get_db), background_tasks: BackgroundTasks = None):
    with transaction(db):
        room = owned(room_id, user, db)
        draft = working(room, db)
        if data.revision != draft.revision: raise HTTPException(409, 'This draft changed. Refresh before saving; your open form has not been applied.')
        doc = deepcopy(draft.document)
        apply_edit(doc, data.action, data.data, room_id)
        draft.document = doc
        draft.revision += 1
        draft.dirty = True
        draft.updated_at = datetime.utcnow()
        db.add(draft)
        db.flush()
        result = response(room, draft, db)
    if background_tasks is not None and data.action == 'metadata' and 'exposed_ports' in data.data:
        from app.network_sync import sync_room_background
        background_tasks.add_task(sync_room_background, room_id)
    return result


def install_content(room, doc, db):
    """Keep IDs for editorial changes; fork grading identities for new rubrics."""
    old_tasks = list(db.scalars(select(RoomTask).where(RoomTask.room_id == room.id)))
    old_questions = list(db.scalars(select(ChallengeQuestion).where(ChallengeQuestion.room_id == room.id, ChallengeQuestion.is_active.is_(True))))
    for i, task in enumerate(old_tasks): task.display_order = 1000 + i
    for i, q in enumerate(old_questions): q.display_order = 1000 + i
    db.flush()
    tasks = {t.id: t for t in old_tasks}
    task_ids = {}
    for value in doc['tasks']:
        task = tasks.get(value['id'])
        if task is None:
            task = RoomTask(room_id=room.id)
            db.add(task)
        for k in ('title', 'instructions', 'display_order'): setattr(task, k, value[k])
        db.flush()
        task_ids[value['id']] = task.id
    questions = {q.id: q for q in old_questions}
    retained = set()
    for value in doc['questions']:
        q = questions.get(value['id'])
        if q and (q.points != value['points'] or q.answer_digest != value['answer_digest'] or q.answer_salt != value['answer_salt']):
            q.is_active = False
            db.flush()
            q = None
        if q is None:
            q = ChallengeQuestion(room_id=room.id, is_active=True)
            db.add(q)
        for k in ('question_text', 'hint', 'points', 'display_order', 'answer_salt', 'answer_digest'): setattr(q, k, value[k])
        q.answer_ciphertext = value.get('answer_ciphertext')
        q.task_id = task_ids.get(value['task_id'])
        db.flush()
        retained.add(q.id)
    for q in old_questions:
        if q.id not in retained: q.is_active = False
    db.flush()
    for task in old_tasks:
        if task.id not in task_ids.values():
            db.execute(update(ChallengeQuestion).where(ChallengeQuestion.task_id == task.id).values(task_id=None))
            db.delete(task)
    for k, v in doc['metadata'].items():
        if k != 'categories':setattr(room, k, v)
    from app.categories import set_room_categories, SLUGS, legacy_category
    values = doc['metadata'].get('categories', [doc['metadata']['category']])
    # Older release/draft snapshots can predate the canonical taxonomy. Keep the
    # original audit value; request writes still accept canonical slugs only.
    if any(value not in SLUGS for value in values):
        if room.category_original is None:room.category_original = doc['metadata']['category']
        values = list(dict.fromkeys(legacy_category(value) for value in values))
    set_room_categories(room, values)
    db.flush()


@router.post('/{room_id}/workspace/publish', response_model=WorkspaceResponse)
def publish_workspace(room_id: int, data: PublishWrite, user: User = Depends(get_current_user), db: Session = Depends(get_db), background_tasks: BackgroundTasks = None):
    with transaction(db):
        room = owned(room_id, user, db)
        draft = working(room, db)
        if not draft.dirty and room.status == 'published' and data.revision in (draft.revision, draft.last_published_revision):
            return response(room, draft, db)
        if data.revision != draft.revision: raise HTTPException(409, 'The draft changed. Review it before publishing.')
        template = validate_room(db, room, draft.image_template_id, updating=True)
        # Preserve the pre-workspace publication as a baseline, without inventing
        # attribution for legacy solves or changing their timestamps.
        if room.status == 'published' and not db.scalar(select(RoomRelease.id).where(RoomRelease.room_id == room.id).limit(1)):
            from app.room_media import register_release_media
            release = RoomRelease(room_id=room.id, document=document(room, db), image_template_id=image_selection(room, db), published_by=room.creator_id)
            db.add(release)
            register_release_media(db, release, release.document)
        install_content(room, draft.document, db)
        if template:
            db.execute(update(RoomTemplate).where(RoomTemplate.room_id == room.id).values(is_published=False))
            db.flush()
            template.is_published = True
        room.status = 'published'
        room.updated_at = datetime.utcnow()
        published_doc = document(room, db)
        from app.room_media import register_release_media
        release = RoomRelease(room_id=room.id, document=deepcopy(published_doc), image_template_id=draft.image_template_id, published_by=user.id)
        db.add(release)
        register_release_media(db, release, release.document)
        draft.document = published_doc
        draft.last_published_revision = draft.revision
        draft.revision += 1
        draft.dirty = False
        draft.updated_at = datetime.utcnow()
        db.add(draft)
        db.flush()
        result = response(room, draft, db)
    from app.network_sync import sync_room_background
    if background_tasks is not None:
        background_tasks.add_task(sync_room_background, room_id)
    return result
