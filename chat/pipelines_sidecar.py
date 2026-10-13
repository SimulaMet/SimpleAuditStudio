"""Launch and stop the Studio Pipelines filter service as a background process.

Mirrors the Caddy front-door lifecycle in ``chat/proxy.py``: start it with a
ready-check + pid file, log to the Open WebUI home dir, and never let a failed
start block the rest of the stack. The service itself is
``chat/pipelines_service.py`` (a small FastAPI app); this module only manages
its process.

It is opt-in via SIMPLEAUDIT_PIPELINES (default off) so existing deployments are
untouched. When enabled, Open WebUI's "openai api url" should point at this
service so every chat flows through it into Langfuse — see docs/chat.md.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_process: subprocess.Popen | None = None
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("SIMPLEAUDIT_PIPELINES", "").strip().lower() not in {
        "", "off", "disabled", "false", "no", "0",
    }


def port() -> int:
    return int(os.environ.get("SIMPLEAUDIT_PIPELINES_PORT", "9099"))


def _pid_file() -> Path:
    from chat.proxy import home_dir

    return home_dir() / "pipelines.pid"


def _ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/models", timeout=1
            ) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.3)
    return False


def _log_path() -> Path:
    from chat.proxy import home_dir

    path = home_dir() / "pipelines.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def start() -> subprocess.Popen | None:
    """Start the pipelines filter service if enabled; return its process or None."""
    global _process
    if not enabled():
        return None
    with _lock:
        if _process is not None and _process.poll() is None:
            return _process
        from simpleaudit_studio.process_guard import guarded_command

        # Point the service at Studio's model connections when an upstream is set;
        # otherwise it answers canned completions (pure capture).
        env = dict(os.environ)
        env.setdefault("SIMPLEAUDIT_PIPELINES_HOST", "127.0.0.1")

        log = _log_path().open("a")
        log.write(f"\n--- studio pipelines start (:{port()}) ---\n")
        log.flush()
        cmd = guarded_command([sys.executable, "-m", "chat.pipelines_service"])
        process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, env=env)
        if not _ready(port()):
            log.close()
            _terminate(process.pid)
            logger.warning("Pipelines service did not bind :%d; leaving it stopped", port())
            return None
        _process = process
        try:
            _pid_file().write_text(str(process.pid))
        except OSError:
            pass
        logger.info("Studio pipelines filter on :%d (PID %d)", port(), process.pid)
        return process


def stop() -> None:
    """Stop the service this process started. Safe to call more than once."""
    global _process
    with _lock:
        process, _process = _process, None
        if process is not None and process.poll() is None:
            _terminate(process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Pipelines service (PID %d) did not exit", process.pid)
        try:
            if _pid_file().exists() and _pid_file().read_text().strip() == str(process.pid):
                _pid_file().unlink(missing_ok=True)
        except OSError:
            pass


def _terminate(pid: int) -> None:
    from chat.proxy import _terminate as _term

    _term(pid)
