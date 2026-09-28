"""Minimal config helpers for single-process deployments.

Used by `uvx simpleaudit-studio` and the HF Space Dockerfile. In minimal
config:
- Django uses SQLite (SIMPLEAUDIT_MINIMAL=1)
- Hatchet runs in embedded mode (sidecar binary + embedded Postgres)
- The worker runs in the main thread alongside the web server
- No external services required (no standalone Postgres, no supervisord)
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level state for the embedded Hatchet client
_embedded_client: Any = None
_embedded_lock = threading.Lock()


def is_minimal_config() -> bool:
    """Return True when running in minimal config (single-process, no external services)."""
    return os.environ.get("SIMPLEAUDIT_MINIMAL", "").strip() == "1"


def _data_dir() -> str:
    """Return (and create) the persistent data dir for the embedded Postgres.

    A fixed location avoids re-running initdb (~218M, ~15s) on every start.
    Override with SIMPLEAUDIT_EMBEDDED_PG_DIR if needed.
    """
    d = os.path.expanduser(
        os.environ.get("SIMPLEAUDIT_EMBEDDED_PG_DIR", "~/.simpleaudit-studio/embedded-pg")
    )
    os.makedirs(d, exist_ok=True)
    return d


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_stale_embedded_pg(data_dir: str) -> None:
    """Remove a stale ``postmaster.pid`` left by a hard-killed previous run.

    If a prior dev server / CLI was killed (SIGKILL, crash, terminal close),
    its bundled Postgres can leave ``data/postmaster.pid`` behind. The next
    sidecar then fails to start with ``FATAL: lock file "postmaster.pid"
    already exists``. We only remove the lock when the recorded postmaster PID
    is no longer running — a live Postgres is never touched.

    Best-effort: any error is logged and swallowed so startup proceeds.
    """
    pid_file = Path(data_dir) / "data" / "postmaster.pid"
    try:
        if not pid_file.exists():
            return
        raw = pid_file.read_text().splitlines()
        if not raw or not raw[0].strip().isdigit():
            return
        pid = int(raw[0].strip())
        if _pid_alive(pid):
            # A real Postgres owns this data dir right now; leave it alone.
            return
        logger.warning(
            "Removing stale embedded Postgres lock %s (recorded PID %d is not running)",
            pid_file, pid,
        )
        pid_file.unlink(missing_ok=True)
    except Exception:  # cleanup must never block startup
        logger.warning("Could not clean up stale embedded Postgres lock", exc_info=True)


def _kill_stale_sidecars() -> None:
    """Terminate orphaned ``hatchet-embedded-sidecar`` processes from dead runs.

    The SDK only reaps sidecars started *in the same process* (via atexit). A
    hard-killed run leaves its sidecar + bundled Postgres alive, holding the
    data dir. We find sidecar processes whose parent is gone (orphaned) and
    terminate them, giving each a moment to shut down its Postgres cleanly.

    Best-effort: any error is logged and swallowed so startup proceeds.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except FileNotFoundError:
        # Minimal containers (e.g. HF Spaces) may lack `ps`; skip silently.
        return
    except Exception:
        logger.warning("Could not enumerate processes for stale sidecar cleanup", exc_info=True)
        return

    me = os.getpid()
    orphans: list[int] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if "hatchet-embedded-sidecar" not in cmd:
            continue
        try:
            pid, ppid = int(pid_s), int(ppid_s)
        except ValueError:
            continue
        if pid == me:
            continue
        # Orphaned = parent no longer exists (init/launchd reparents to 1).
        if not _pid_alive(ppid):
            orphans.append(pid)

    if not orphans:
        return

    logger.warning("Terminating %d orphaned embedded sidecar(s): %s", len(orphans), orphans)
    for pid in orphans:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Give them a moment to shut down their bundled Postgres before we proceed.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(_pid_alive(p) for p in orphans):
        time.sleep(0.2)
    for pid in orphans:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def start_embedded_hatchet() -> Any:
    """Start an embedded Hatchet engine and return the client.

    The sidecar binary downloads on first use (~53 MB) and caches at
    ~/.hatchet/embedded/. The Postgres cluster persists in
    ~/.simpleaudit-studio/embedded-pg/, so only the first start is slow.

    Before starting, cleans up leftovers from a hard-killed previous run
    (orphaned sidecar processes and a stale ``postmaster.pid``) so a restart
    doesn't fail with ``lock file "postmaster.pid" already exists``.

    Returns the Hatchet client instance. Raises on failure.
    """
    global _embedded_client
    with _embedded_lock:
        if _embedded_client is not None:
            return _embedded_client

        data_dir = _data_dir()
        _kill_stale_sidecars()
        _cleanup_stale_embedded_pg(data_dir)

        from hatchet_sdk import ClientConfig, EmbeddedHatchetConfig, Hatchet

        logger.info("Embedded Hatchet data dir: %s", data_dir)

        config = ClientConfig(
            embedded=EmbeddedHatchetConfig(
                postgres_data_dir=data_dir,
                ready_timeout_seconds=120.0,
            )
        )

        print("\n⏳ Starting embedded Hatchet engine (first run may take ~15s)...")
        client = Hatchet.from_embedded(config)
        _embedded_client = client
        print("✅ Hatchet engine ready.\n")
        return client


def stop_embedded_hatchet() -> None:
    """Stop the embedded Hatchet engine cleanly."""
    global _embedded_client
    with _embedded_lock:
        if _embedded_client is not None:
            try:
                _embedded_client.stop_embedded()
            except Exception:
                logger.warning("Error stopping embedded Hatchet", exc_info=True)
            _embedded_client = None


def get_embedded_client() -> Any | None:
    """Return the running embedded Hatchet client, or None."""
    return _embedded_client


# Best-effort clean shutdown even if the caller forgets to stop explicitly.
atexit.register(stop_embedded_hatchet)
