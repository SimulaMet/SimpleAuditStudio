"""The forward-auth proxy that fronts Open WebUI in embedded mode.

Docker deployments use Caddy for this (see deploy/compose/Caddyfile.chat); this
module is the no-Docker equivalent, so ``uvx simpleaudit-studio`` needs nothing
but Python. Both do the same three things:

  1. strip any client-supplied trusted header (otherwise anyone could forge one),
  2. ask Studio ``GET /chat/authz`` who the browser is, forwarding its cookies,
  3. forward the request to Open WebUI with the returned headers added.

WebSockets are not proxied: an ``Upgrade`` request gets 501, which makes Open
WebUI's Socket.IO client stay on its HTTP long-polling transport. Chat responses
stream over plain HTTP (SSE) and are unaffected.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from infra import chat

logger = logging.getLogger(__name__)

# Open WebUI is managed like the embedded Hatchet engine: one instance per
# process, stopped on the way out (atexit as well as the CLI's own shutdown),
# and a run that was hard-killed has its leftovers cleaned up by the next start.
_process: subprocess.Popen | None = None
_log_file = None
_process_lock = threading.Lock()

# Connection-level headers that must not be forwarded (RFC 9110 §7.6.1).
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})
_STRIP_FROM_REQUEST = _HOP_BY_HOP | {h.lower() for h in chat.TRUSTED_HEADERS}
# Dropped from the response so the iframe in Studio is allowed to render it.
# X-Frame-Options has no origin allow-list, so it can only be removed.
_STRIP_FROM_RESPONSE = _HOP_BY_HOP | {"x-frame-options"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    studio_url = "http://127.0.0.1:8000"    # set by serve()
    client: httpx.Client                     # set by serve()

    def log_message(self, fmt, *args):
        logger.debug("chat-proxy %s", fmt % args)

    def _proxy(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_error(501, "WebSocket not proxied")
            return

        headers = {k: v for k, v in self.headers.items() if k.lower() not in _STRIP_FROM_REQUEST}
        # The body is forwarded byte for byte, so the upstream may only use an
        # encoding this client asked for. Without this httpx adds its own
        # Accept-Encoding and the client gets gzip it cannot read.
        if not any(k.lower() == "accept-encoding" for k in headers):
            headers["Accept-Encoding"] = "identity"
        cookie = self.headers.get("Cookie", "")

        identity = self._identify(cookie)
        if identity is None:
            self.send_response(302)
            self.send_header("Location", f"{self.studio_url}/chat/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        headers.update(identity)

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        try:
            with self.client.stream(
                self.command, chat.UPSTREAM + self.path, headers=headers, content=body,
            ) as upstream:
                self.send_response(upstream.status_code)
                for key, value in upstream.headers.multi_items():
                    if key.lower() not in _STRIP_FROM_RESPONSE:
                        self.send_header(key, value)
                # Responses are streamed without a known length (SSE included),
                # so the connection delimits the body.
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                for chunk in upstream.iter_raw():
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except httpx.HTTPError as exc:
            logger.warning("chat upstream error: %s", exc)
            self.send_error(502, "Chat backend unavailable")
        except (BrokenPipeError, ConnectionResetError):
            pass    # the browser navigated away mid-stream

    def _identify(self, cookie: str) -> dict[str, str] | None:
        """Ask Studio who this browser is. None when signed out."""
        try:
            response = self.client.get(
                f"{self.studio_url}/chat/authz",
                headers={"Cookie": cookie} if cookie else {},
            )
        except httpx.HTTPError as exc:
            logger.warning("chat authz unreachable: %s", exc)
            return None
        if response.status_code != 200:
            return None
        return {h: response.headers[h] for h in chat.TRUSTED_HEADERS if h in response.headers}

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _proxy


def serve(studio_port: int) -> ThreadingHTTPServer:
    """Start the proxy on chat.PROXY_PORT in a daemon thread."""
    _Handler.studio_url = f"http://127.0.0.1:{studio_port}"
    _Handler.client = httpx.Client(timeout=httpx.Timeout(None, connect=10.0), follow_redirects=False)
    server = ThreadingHTTPServer(("0.0.0.0", chat.PROXY_PORT), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def home_dir() -> Path:
    """Open WebUI's data folder, beside Studio's own."""
    from simpleaudit_studio.paths import data_dir

    return data_dir() / "openwebui"


def log_path() -> Path:
    """Where Open WebUI's own output goes — it is far too chatty for the console."""
    return home_dir() / "server.log"


def is_first_run() -> bool:
    """True when Open WebUI has never started here, so it has to be fetched."""
    return not (home_dir() / "webui.db").exists()


def pid_file() -> Path:
    """Records the running Open WebUI, so the next start can clean up after a
    run that never got to stop it."""
    return home_dir() / "open-webui.pid"


def _stop_stale() -> None:
    """Stop an Open WebUI left behind by a hard-killed run.

    It holds the upstream port, so the next start would fail to bind. Only a
    leftover is touched: a process whose parent is gone. One with a live parent
    belongs to another running Studio and is left alone (that start then fails
    on the port, which is the honest outcome).

    Best-effort: any error is logged and swallowed so startup proceeds.
    """
    from infra.minimal_config import _orphaned, _parent_pid, _pid_alive

    file = pid_file()
    try:
        if not file.exists():
            return
        raw = file.read_text().strip()
        pid = int(raw) if raw.isdigit() else 0
        if pid and _pid_alive(pid):
            parent = _parent_pid(pid)
            if parent is not None and not _orphaned(parent):
                return
            logger.warning("Stopping orphaned Open WebUI (PID %d) left by a killed run", pid)
            _terminate(pid)
        file.unlink(missing_ok=True)
    except Exception:    # cleanup must never block startup
        logger.warning("Could not clean up a leftover Open WebUI", exc_info=True)


def _terminate(pid: int, grace: float = 10.0) -> None:
    """SIGTERM the process group, then SIGKILL whatever is still there.

    The group matters: ``uvx open-webui`` is a launcher with the real server as
    its child, so signalling only the launcher leaves the server running.
    """
    from infra.minimal_config import _pid_alive, _wait_gone

    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        if not _pid_alive(pid):
            return
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        except (AttributeError, OSError):
            # No process groups (Windows): fall back to the process itself.
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
        _wait_gone([pid], wait)


def stop_open_webui() -> None:
    """Stop the Open WebUI this process started. Safe to call more than once."""
    global _process, _log_file
    with _process_lock:
        process, _process = _process, None
        if process is not None and process.poll() is None:
            _terminate(process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Open WebUI (PID %d) did not exit", process.pid)
        if _log_file is not None:
            _log_file.close()
            _log_file = None
        if process is not None:
            pid_file().unlink(missing_ok=True)


# Best-effort clean shutdown even if the caller forgets to stop explicitly.
atexit.register(stop_open_webui)


def wait_until_ready(process: subprocess.Popen, timeout: float = 900.0) -> bool:
    """Poll Open WebUI until it answers, the process dies, or time runs out.

    Uses urllib rather than httpx so a start-up that takes minutes does not
    write a request log line every two seconds to the console.
    """
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(chat.UPSTREAM + "/health", timeout=3.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2.0)
    return False


def start_open_webui() -> subprocess.Popen:
    """Start Open WebUI bound to loopback, in trusted-header mode.

    Uses the ``open-webui`` command when it is installed, otherwise ``uvx``
    fetches it on first run. SIMPLEAUDIT_CHAT_CMD overrides both.

    One instance per process: a second call returns the running one. Leftovers
    from a hard-killed previous run are stopped first.
    """
    with _process_lock:
        if _process is not None and _process.poll() is None:
            return _process

        home = home_dir()
        home.mkdir(parents=True, exist_ok=True)
        _stop_stale()
        return _spawn(home)


def _spawn(home: Path) -> subprocess.Popen:
    """Build the command and environment, and start the server."""
    global _process, _log_file

    host = urlsplit(chat.UPSTREAM).hostname or "127.0.0.1"
    port = urlsplit(chat.UPSTREAM).port or 8080

    command = os.environ.get("SIMPLEAUDIT_CHAT_CMD")
    if command:
        argv = command.split()
    elif shutil.which("open-webui"):
        argv = ["open-webui", "serve"]
    elif shutil.which("uvx"):
        argv = ["uvx", "open-webui", "serve"]
    else:
        raise RuntimeError(
            "Open WebUI not found. Install it (`uv tool install open-webui`) or set "
            "SIMPLEAUDIT_CHAT_CMD to the command that starts it."
        )
    if not command:
        # `open-webui serve` ignores HOST/PORT and defaults to 0.0.0.0:8080. The
        # bind address is the whole security model here — anything that can reach
        # it can claim any identity — so it must come from the flags.
        argv += ["--host", host, "--port", str(port)]

    env = {
        **os.environ,
        "DATA_DIR": str(home),
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": chat.EMAIL_HEADER,
        "WEBUI_AUTH_TRUSTED_NAME_HEADER": chat.NAME_HEADER,
        "WEBUI_AUTH_TRUSTED_ROLE_HEADER": chat.ROLE_HEADER,
        "ENABLE_SIGNUP": "false",
        "WEBUI_URL": chat.PUBLIC_URL,
    }
    # Open WebUI keeps its signing key in ``.webui_secret_key`` in the working
    # directory, with no setting for it: running it from its own data folder
    # keeps that out of wherever Studio was started and stable across restarts
    # (a new key signs every session out).
    _log_file = log_path().open("a")
    # Its own process group, so stopping it reaches the server that `uvx` (or
    # any other launcher) starts as a child, not just the launcher.
    _process = subprocess.Popen(
        argv, env=env, cwd=str(home),
        stdout=_log_file, stderr=subprocess.STDOUT, start_new_session=True,
    )
    pid_file().write_text(str(_process.pid))
    return _process
