"""The learner's accepted input; never read the creator's authoring ciphertext."""
import base64
import binascii
import secrets
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from app.editable_answers import cipher


def context(room_id, question_id, learner_id):
    return f'secforge:accepted-submission:v1:{room_id}:{question_id}:{learner_id}'.encode('ascii')


def encrypt_submission(answer, room_id, question_id, learner_id):
    nonce = secrets.token_bytes(12)
    body = cipher().encrypt(nonce, answer.encode('utf-8'), context(room_id, question_id, learner_id))
    return 'v1.' + base64.urlsafe_b64encode(nonce + body).decode('ascii')


def decrypt_submission(value, room_id, question_id, learner_id):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not value.startswith('v1.') or len(value) > 24576:
            raise ValueError()
        body = base64.b64decode(value[3:], altchars=b'-_', validate=True)
        if len(body) < 29:
            raise ValueError()
        return cipher().decrypt(body[:12], body[12:], context(room_id, question_id, learner_id)).decode('utf-8')
    except (InvalidTag, ValueError, UnicodeError, binascii.Error):
        raise HTTPException(503, 'Saved submissions could not be opened. Please retry or contact an administrator.') from None
