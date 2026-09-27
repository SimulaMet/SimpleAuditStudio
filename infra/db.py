"""Database resilience for background work (worker tasks, sweeper).

1. ``retry_if_locked``: retry writes that hit a transient lock instead of failing.
2. ``with_fresh_connection``: Django only recycles connections around HTTP
   requests; worker threads would otherwise keep one connection forever and
   fail every task after the database restarts or drops an idle connection.

Locks:

SQLite (local dev and the demo) allows one writer at a time: when the web
server and several worker threads write together, a write can still time out
with "database is locked" despite the busy timeout. PostgreSQL can report a
deadlock or serialization failure. Both are safe to retry: the statement did
not commit. Writes inside an outer transaction re-raise, so the outermost
retried call (which owns the transaction) replays the whole unit.
"""
from __future__ import annotations

import functools
import logging
import random
import time

from django.db import OperationalError, close_old_connections, connection

logger = logging.getLogger(__name__)

_TRANSIENT = ("database is locked", "database table is locked", "database is busy", "deadlock detected",
              "could not serialize access")
ATTEMPTS = 8


def is_transient_db_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, OperationalError) and any(marker in text for marker in _TRANSIENT)


def retry_if_locked(fn):
    """Decorator: retry ``fn`` with jittered backoff (~15 s total) on transient lock errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except OperationalError as exc:
                if not is_transient_db_error(exc) or connection.in_atomic_block or attempt == ATTEMPTS:
                    raise
                delay = min(0.1 * 2 ** attempt, 4.0) * (0.5 + random.random())
                logger.warning("%s hit a busy database (%s); retry %d/%d in %.1fs",
                               fn.__qualname__, exc, attempt, ATTEMPTS - 1, delay)
                time.sleep(delay)
        return None  # unreachable

    return wrapper


def with_fresh_connection(fn):
    """Decorator for code run outside a request: drop stale connections before and after."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()

    return wrapper
