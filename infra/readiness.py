"""Readiness checks for the web service."""
import logging
from functools import lru_cache

from django.db import connection

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def migrations_pending() -> bool:
    """Check whether any Django migrations are unapplied.

    The result is cached per process because migration state changes only when
    an operator runs migrations, not during normal request handling.
    """
    from django.core.management import call_command
    from io import StringIO

    out = StringIO()
    try:
        call_command("showmigrations", stdout=out)
    except Exception:
        logger.exception("readiness migration check failed")
        return True

    pending = 0
    for line in out.getvalue().splitlines():
        if line.startswith("[ ]"):
            pending += 1
    return pending > 0


def database_ready() -> tuple[bool, str]:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, "ok"
    except Exception as exc:
        logger.exception("readiness database check failed")
        return False, str(exc)


def ready_payload() -> dict:
    db_ok, db_detail = database_ready()
    checks = {"database": db_detail}
    if not db_ok:
        return {"status": "unavailable", "checks": checks}

    if migrations_pending():
        checks["migrations"] = "pending"
        return {"status": "unavailable", "checks": checks}

    checks["migrations"] = "ok"
    return {"status": "ready", "checks": checks}
