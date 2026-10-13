"""Gunicorn configuration for the SimpleAudit Studio web service.

The audit-run live events endpoint (``audits.views`` ``event_stream``) is a
Server-Sent Events stream that can stay open for up to an hour. Gunicorn's
default ``sync`` worker cannot heartbeat while serving a request, so the
arbiter SIGABRTs it after ``timeout`` seconds (default 30), killing the stream
and raising ``SystemExit: 1``. The threaded ``gthread`` worker heartbeats from
its main loop independently of in-flight requests, so long-lived streams are
safe. Each open stream occupies one thread, so ``workers * threads`` bounds
the number of concurrent stream viewers plus regular requests.
"""

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
workers = _int_env("GUNICORN_WORKERS", 2)
worker_class = "gthread"
threads = _int_env("GUNICORN_THREADS", 16)
timeout = _int_env("GUNICORN_TIMEOUT", 60)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _int_env("GUNICORN_KEEPALIVE", 5)
