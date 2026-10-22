"""UTC time policy for learner sessions; creator lifetimes remain independent."""
from datetime import datetime, timedelta, timezone

INITIAL_TIME = timedelta(minutes=60)
EXTENSION_TIME = timedelta(minutes=60)
EXTENSION_WINDOW = timedelta(minutes=30)
MAX_EXTENSIONS = 2
MAX_TIME = timedelta(minutes=180)


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    # Historical application timestamps and SQLite timestamps represent UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def deadline_passed(lab, now=None):
    return as_utc(lab.expires_at) <= (now or utc_now())
