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

import logging
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from infra import chat

logger = logging.getLogger(__name__)

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
    """
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
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
    log = log_path().open("a")
    return subprocess.Popen(argv, env=env, cwd=str(home), stdout=log, stderr=subprocess.STDOUT)
