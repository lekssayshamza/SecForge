from app.permissions import has_creator_access, has_learning_access
"""Room learning interactions. No infrastructure operations or plaintext answer storage."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import math
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import ChallengeQuestion, QuestionSolve, Room, RoomProgress, RoomTask, User
from app.rooms import get_room_or_404
from app.editable_answers import encrypt_answer
from app.learner_answers import encrypt_submission, decrypt_submission
from pydantic import BaseModel, Field
from app.schemas import (AnswerSubmission, ProgressResponse, QuestionCreate,
    QuestionListResponse, QuestionOrderRequest, QuestionResponse, QuestionUpdate, SubmissionResponse)


class ConfidentialValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # FastAPI's default validation response echoes input values.
                response = JSONResponse(status_code=422, content={"detail": "Invalid question or answer data."})
            except HTTPException as error:
                error.headers = {**(error.headers or {}), 'Cache-Control': 'private, no-store', 'Pragma': 'no-cache'}
                raise
            response.headers['Cache-Control'] = 'private, no-store'
            response.headers['Pragma'] = 'no-cache'
            return response
        return safe_handler


router = APIRouter(prefix="/rooms", tags=["Challenge questions"], route_class=ConfidentialValidationRoute)
MAX_QUESTIONS = 100
ATTEMPTS_PER_MINUTE = 20


def answer_digest(answer: str, salt: str) -> str:
    # Trim leading/trailing Unicode whitespace only; case and internal whitespace matter.
    root_key = (settings.challenge_answer_key or settings.jwt_secret).encode()
    key = hmac.new(root_key, b"secforge:challenge-answer:v1", hashlib.sha256).digest()
    return hmac.new(key, b"v1\0" + salt.encode() + b"\0" + answer.strip().encode(), hashlib.sha256).hexdigest()


@contextmanager
def transaction(db):
    try:
        yield
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Learning service temporarily unavailable. Please retry.") from None
    except Exception:
        db.rollback()
        raise


def owned_room(room_id, user, db):
    if not has_creator_access(user):
        raise HTTPException(403, "Only room owners or administrators can edit questions.")
    room = db.scalar(select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True))
    if room is None or room.creator_id != user.id:
        raise HTTPException(404, "Room not found")
    from app.room_workspace import legacy_write_guard
    legacy_write_guard(room, db)
    return room


def active_questions(room_id, db):
    return list(db.scalars(select(ChallengeQuestion).where(
        ChallengeQuestion.room_id == room_id, ChallengeQuestion.is_active.is_(True)
    ).order_by(ChallengeQuestion.display_order)))


def authoring_locked(room_id, db):
    return db.scalar(select(QuestionSolve.id).join(ChallengeQuestion).where(
        ChallengeQuestion.room_id == room_id).limit(1)) is not None


def structure_unlocked(room_id, db):
    if authoring_locked(room_id, db):
        raise HTTPException(409, "Learner credit exists. Questions, answers and points are locked; wording, hints and ordering remain editable.")


def reorder(questions, db):
    # Positive temporary positions avoid immediate unique-index conflicts on swaps.
    for i, question in enumerate(questions):
        question.display_order = 1000 + i
    db.flush()
    for i, question in enumerate(questions):
        question.display_order = i + 1
    db.flush()


def selected_question(room_id, question_id, db):
    question = db.get(ChallengeQuestion, question_id)
    if question is None or question.room_id != room_id or not question.is_active:
        raise HTTPException(404, "Question not found")
    return question


def validate_task(room_id, task_id, db):
    if task_id is not None and db.scalar(select(RoomTask.id).where(RoomTask.id == task_id, RoomTask.room_id == room_id)) is None:
        raise HTTPException(422, "Choose a task from this room.")


def questions_response(room_id, db):
    return QuestionListResponse(questions=[QuestionResponse.model_validate(q) for q in active_questions(room_id, db)],
                                authoring_locked=authoring_locked(room_id, db))


@router.get("/{room_id}/questions", response_model=QuestionListResponse)
def list_questions(room_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        get_room_or_404(room_id, user, db)
        return questions_response(room_id, db)
    except SQLAlchemyError:
        raise HTTPException(503, "Learning service temporarily unavailable. Please retry.") from None


@router.post("/{room_id}/questions", response_model=QuestionResponse, status_code=201)
def create_question(room_id: int, data: QuestionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with transaction(db):
        owned_room(room_id, user, db)
        structure_unlocked(room_id, db)
        questions = active_questions(room_id, db)
        if len(questions) >= MAX_QUESTIONS:
            raise HTTPException(409, "A room may contain at most 100 questions.")
        position = data.display_order or len(questions) + 1
        if position > len(questions) + 1:
            raise HTTPException(422, "Display order must fit the question list.")
        validate_task(room_id, data.task_id, db)
        salt = secrets.token_hex(16)
        question = ChallengeQuestion(room_id=room_id, task_id=data.task_id, question_text=data.question_text, hint=data.hint,
            points=data.points, display_order=len(questions) + 1,
            answer_salt=salt, answer_digest=answer_digest(data.answer.get_secret_value(), salt),
            answer_ciphertext=encrypt_answer(data.answer.get_secret_value(), room_id, salt))
        db.add(question)
        db.flush()
        questions.insert(position - 1, question)
        reorder(questions, db)
        response = QuestionResponse.model_validate(question)
    return response


@router.patch("/{room_id}/questions/{question_id}", response_model=QuestionResponse)
def update_question(room_id: int, question_id: int, data: QuestionUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with transaction(db):
        owned_room(room_id, user, db)
        question = selected_question(room_id, question_id, db)
        answer_changed = data.answer is not None and not hmac.compare_digest(answer_digest(data.answer.get_secret_value(), question.answer_salt), question.answer_digest)
        if answer_changed or data.points is not None and data.points != question.points:
            structure_unlocked(room_id, db)
        if "task_id" in data.model_fields_set:
            validate_task(room_id, data.task_id, db); question.task_id = data.task_id
        if data.question_text is not None: question.question_text = data.question_text
        if "hint" in data.model_fields_set: question.hint = data.hint
        if data.points is not None: question.points = data.points
        if data.answer is not None:
            if answer_changed:
                question.answer_salt = secrets.token_hex(16)
                question.answer_digest = answer_digest(data.answer.get_secret_value(), question.answer_salt)
            question.answer_ciphertext = encrypt_answer(data.answer.get_secret_value(), room_id, question.answer_salt)
        if data.display_order is not None:
            questions = active_questions(room_id, db)
            if data.display_order > len(questions): raise HTTPException(422, "Display order must fit the question list.")
            questions.remove(question); questions.insert(data.display_order - 1, question)
            reorder(questions, db)
        db.flush()
        response = QuestionResponse.model_validate(question)
    return response


@router.put("/{room_id}/questions/order", response_model=QuestionListResponse)
def order_questions(room_id: int, data: QuestionOrderRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with transaction(db):
        owned_room(room_id, user, db)
        questions = {q.id: q for q in active_questions(room_id, db)}
        if len(data.question_ids) != len(set(data.question_ids)) or set(data.question_ids) != set(questions):
            raise HTTPException(422, "Provide every active question exactly once.")
        reorder([questions[identity] for identity in data.question_ids], db)
        response = questions_response(room_id, db)
    return response


@router.delete("/{room_id}/questions/{question_id}", status_code=204)
def delete_question(room_id: int, question_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    with transaction(db):
        owned_room(room_id, user, db)
        question = selected_question(room_id, question_id, db)
        structure_unlocked(room_id, db)
        question.is_active = False
        db.flush()
        reorder(active_questions(room_id, db), db)


def progress_response(room_id, learner_id, db, progress=None):
    questions = active_questions(room_id, db)
    ids = {q.id for q in questions}
    solved = list(db.scalars(select(QuestionSolve.question_id).join(ChallengeQuestion).where(
        ChallengeQuestion.room_id == room_id, QuestionSolve.learner_id == learner_id)))
    if progress is None:
        progress = db.scalar(select(RoomProgress).where(RoomProgress.room_id == room_id, RoomProgress.learner_id == learner_id))
    return ProgressResponse(room_id=room_id, solved_question_ids=sorted(solved), questions_solved=len(ids.intersection(solved)),
        questions_total=len(questions), points_earned=progress.points_earned if progress else 0,
        points_total=sum(q.points for q in questions), completed=bool(progress and progress.completed_at),
        completed_at=progress.completed_at if progress else None)


@router.get("/{room_id}/progress", response_model=ProgressResponse)
def get_progress(room_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not has_learning_access(user): raise HTTPException(403, "Learner access required")
    try:
        get_room_or_404(room_id, user, db)
        return progress_response(room_id, user.id, db)
    except SQLAlchemyError:
        raise HTTPException(503, "Learning service temporarily unavailable. Please retry.") from None


@router.post("/{room_id}/questions/{question_id}/submit", response_model=SubmissionResponse)
def submit_answer(room_id: int, question_id: int, data: AnswerSubmission, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not has_learning_access(user): raise HTTPException(403, "Learner access required")
    with transaction(db):
        # Real PostgreSQL row locking serializes authoring and awards, independent of process count.
        room = db.scalar(select(Room).where(Room.id == room_id).with_for_update().execution_options(populate_existing=True))
        if room is None or room.status != "published": raise HTTPException(404, "Room not found")
        if room.creator_id == user.id:
            raise HTTPException(403, "Your own room is a non-scoring preview. Test it through the creator workspace.")
        question = selected_question(room_id, question_id, db)
        progress = db.scalar(select(RoomProgress).where(RoomProgress.room_id == room_id, RoomProgress.learner_id == user.id))
        now = datetime.utcnow()
        if progress is None:
            progress = RoomProgress(room_id=room_id, learner_id=user.id, points_earned=0, attempt_count=0, attempt_window_started_at=now)
            db.add(progress)
        elapsed = (now - progress.attempt_window_started_at).total_seconds()
        if elapsed >= 60:
            progress.attempt_window_started_at = now; progress.attempt_count = 0
        if progress.attempt_count >= ATTEMPTS_PER_MINUTE:
            raise HTTPException(429, "Too many attempts. Wait before trying again.", headers={"Retry-After": str(max(1, math.ceil(60 - elapsed)))})
        progress.attempt_count += 1
        correct = hmac.compare_digest(question.answer_digest, answer_digest(data.answer.get_secret_value(), question.answer_salt))
        already_solved = db.scalar(select(QuestionSolve.id).where(QuestionSolve.question_id == question.id, QuestionSolve.learner_id == user.id)) is not None
        awarded = 0
        if correct and not already_solved:
            awarded = question.points
            from app.models import RoomRelease
            release_id = db.scalar(select(RoomRelease.id).where(RoomRelease.room_id == room_id).order_by(RoomRelease.id.desc()).limit(1))
            encrypted = encrypt_submission(data.answer.get_secret_value(), room_id, question.id, user.id)
            db.add(QuestionSolve(question_id=question.id, learner_id=user.id, points_awarded=awarded, solved_at=now, release_id=release_id, submission_ciphertext=encrypted))
            progress.points_earned += awarded
        db.flush()
        response_progress = progress_response(room_id, user.id, db, progress)
        if response_progress.questions_total > 0 and response_progress.questions_solved == response_progress.questions_total and progress.completed_at is None:
            progress.completed_at = now
            from app.models import RoomRelease
            progress.completed_release_id = db.scalar(select(RoomRelease.id).where(RoomRelease.room_id == room_id).order_by(RoomRelease.id.desc()).limit(1))
            response_progress.completed = True; response_progress.completed_at = now
        response = SubmissionResponse(correct=correct, already_solved=already_solved, points_awarded=awarded, progress=response_progress)
    return response


class AcceptedSubmission(BaseModel):
    question_id: int
    answer: str | None = Field(repr=False)
    points_awarded: int
    legacy: bool


@router.get('/{room_id}/accepted-answers', response_model=list[AcceptedSubmission])
def accepted_answers(room_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not has_learning_access(user):
        raise HTTPException(403, 'Learner access required')
    try:
        get_room_or_404(room_id, user, db)
        solves = db.scalars(select(QuestionSolve).join(ChallengeQuestion).where(
            ChallengeQuestion.room_id == room_id, ChallengeQuestion.is_active.is_(True),
            QuestionSolve.learner_id == user.id).order_by(ChallengeQuestion.display_order))
        return [AcceptedSubmission(question_id=s.question_id,
            answer=decrypt_submission(s.submission_ciphertext, room_id, s.question_id, user.id),
            points_awarded=s.points_awarded, legacy=s.submission_ciphertext is None) for s in solves]
    except SQLAlchemyError:
        raise HTTPException(503, 'Saved submissions temporarily unavailable. Please retry.') from None
