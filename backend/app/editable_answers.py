"""Recoverable authoring copy only. Grading still uses the existing HMAC verifier."""
import base64
import binascii
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from app.config import settings


def cipher():
    try:
        key = base64.b64decode(settings.editable_answer_key.get_secret_value(), altchars=b'-_', validate=True)
        if len(key) != 32:
            raise ValueError()
        return AESGCM(key)
    except (ValueError, binascii.Error):
        raise HTTPException(503, 'Editable answer storage is unavailable. Please contact an administrator.') from None


def associated_data(room_id, salt):
    # The salt is an immutable grading identity shared by its draft/live copies.
    return f'secforge:editable-answer:v1:{room_id}:{salt}'.encode('ascii')


def encrypt_answer(answer, room_id, salt):
    nonce = secrets.token_bytes(12)
    body = cipher().encrypt(nonce, answer.strip().encode('utf-8'), associated_data(room_id, salt))
    return 'v1.' + base64.urlsafe_b64encode(nonce + body).decode('ascii')


def decrypt_answer(value, room_id, salt):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not value.startswith('v1.') or len(value) > 4096:
            raise ValueError()
        body = base64.b64decode(value[3:], altchars=b'-_', validate=True)
        if len(body) < 29:
            raise ValueError()
        return cipher().decrypt(body[:12], body[12:], associated_data(room_id, salt)).decode('utf-8')
    except (InvalidTag, ValueError, UnicodeError, binascii.Error):
        # Never turn unreadable ciphertext into an empty/legacy answer.
        raise HTTPException(503, 'The saved answer could not be opened. Please contact an administrator.') from None
