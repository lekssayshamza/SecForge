"""Per-resource single-flight guards for long-running lifecycle operations.

Prevents two Terraform operations from running concurrently against the same
builder/lab instance, regardless of which code path started them (HTTP routes,
FastAPI background tasks, or the reconciliation worker). This is the fix for
the observed "Error acquiring the state lock" lost-update race where the
background stop task and the crashed-stopping reconciler handler both destroyed
the same lab in parallel.

This is an in-process guard. SecForge currently runs Terraform from a single
uvicorn process; if it ever moves to multiple workers or processes, this must
be replaced by a database-backed lease.
"""

import threading
from contextlib import contextmanager
from typing import Iterator


class OperationInProgress(Exception):
    """Raised when a single-flight guard is already held for a resource."""


# Guards are reused per resource for the lifetime of the process so that a
# subsequent operation on the same resource observes the previous one.
# _REGISTRY_LOCK guards the dict itself so two threads racing to create the
# lock for the same key end up sharing exactly one lock object.
_REGISTRY_LOCK = threading.Lock()
_RESOURCE_LOCKS: dict[str, threading.Lock] = {}


def lab_lock_key(lab_id: int) -> str:
    return f"lab:{lab_id}"


def builder_lock_key(builder_id: int) -> str:
    return f"builder:{builder_id}"


@contextmanager
def single_flight(key: str) -> Iterator[None]:
    """Acquire a non-blocking exclusive guard for ``key``.

    Raises ``OperationInProgress`` if another operation holds the guard for the
    same key. The guard is always released in a ``finally`` block, so it is
    released even when the guarded body raises.
    """
    with _REGISTRY_LOCK:
        lock = _RESOURCE_LOCKS.setdefault(key, threading.Lock())
        acquired = lock.acquire(blocking=False)
    if not acquired:
        raise OperationInProgress(
            f"Another operation is already in progress for {key!r}"
        )
    try:
        yield
    finally:
        lock.release()