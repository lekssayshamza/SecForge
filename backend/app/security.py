from datetime import datetime, timedelta, timezone

import threading
from contextlib import contextmanager
from fastapi import HTTPException

import jwt
from argon2 import PasswordHasher

from app.config import settings


password_hasher = PasswordHasher()
# The production host has 2GB RAM. Argon2's default work needs 64MiB per
# invocation; an unbounded web thread pool can exhaust it before IP quotas help.
_password_slots = threading.BoundedSemaphore(2)

@contextmanager
def _password_work():
    if not _password_slots.acquire(blocking=False):
        raise HTTPException(503, 'Sign-in is busy. Please retry shortly.', headers={'Retry-After': '2'})
    try:
        yield
    finally:
        _password_slots.release()



def hash_password(password: str) -> str:
    with _password_work():
        return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    with _password_work():
        try:
            password_hasher.verify(password_hash, password)
            return True
        except Exception:
            return False


def create_access_token(user_id: int, role: str, token_version: int = 0) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )

    payload = {
        "sub": str(user_id),
        "role": role,
        "ver": token_version,
        "exp": expires_at,
    }

    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub"]},
    )
