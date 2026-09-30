"""The forward-auth proxy that fronts Open WebUI in embedded mode.

Docker deployments use Caddy for this (see deploy/compose/Caddyfile.chat); this
module is the no-Docker equivalent, so ``uvx simpleaudit-studio`` needs nothing
but Python. Both do the same three things:

  1. strip any client-supplied trusted header (otherwise anyone could forge one),
  2. ask Studio ``GET /chat/authz`` who the browser is, forwarding its cookies,
  3. forward the request to Open WebUI with the returned headers added.

WebSocket upgrades are tunnelled: the handshake is forwarded with the identity
headers attached, and once the upstream answers 101 the two sockets are simply
piped together. Open WebUI's Socket.IO then behaves as it does behind Caddy,
rather than falling back to long-polling and logging failed upgrades.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from chat import config as chat

logger = logging.getLogger(__name__)

# Open WebUI is managed like the embedded Hatchet engine: one instance per
# process, stopped on the way out (atexit as well as the CLI's own shutdown),
# and a run that was hard-killed has its leftovers cleaned up by the next start.
_process: subprocess.Popen | None = None
_log_file = None
_process_lock = threading.Lock()

# Open WebUI's page pulls dozens of assets, and each one would otherwise ask
# Studio who the browser is. The answer is cached for a moment, keyed on the
# exact cookie header, so a page load costs one authz request instead of fifty.
# Short on purpose: a sign-out takes effect within this window.
IDENTITY_TTL = float(os.environ.get("SIMPLEAUDIT_CHAT_IDENTITY_TTL", "5"))
_IDENTITY_CACHE_LIMIT = 512
_identity_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
_identity_lock = threading.Lock()


def _cached_identity(cookie: str) -> tuple[bool, dict[str, str] | None]:
    """(hit, identity). A miss and a cached "signed out" look different."""
    if IDENTITY_TTL <= 0:
        return False, None
    with _identity_lock:
        entry = _identity_cache.get(cookie)
        if entry is None or entry[0] < time.monotonic():
            return False, None
        return True, entry[1]


def _remember_identity(cookie: str, identity: dict[str, str] | None) -> None:
    if IDENTITY_TTL <= 0:
        return
    with _identity_lock:
        if len(_identity_cache) >= _IDENTITY_CACHE_LIMIT:
            _identity_cache.clear()    # cheap and rare; entries are seconds old
        _identity_cache[cookie] = (time.monotonic() + IDENTITY_TTL, identity)

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
    transport: httpx.HTTPTransport           # set by serve()

    def client(self) -> httpx.Client:
        """A client for one request, over the shared connection pool.

        Never a client shared between requests: httpx clients keep a cookie jar,
        and one jar here would mean one browser's session cookie — or Open
        WebUI's token — being sent on the next browser's request. Every identity
        this proxy forwards must come from the request it is handling, and
        nothing may be remembered between them.
        """
        return httpx.Client(
            transport=self.transport,
            timeout=httpx.Timeout(None, connect=10.0),
            follow_redirects=False,
        )

    def log_message(self, fmt, *args):
        logger.debug("chat-proxy %s", fmt % args)

    def _proxy(self):
        # Studio's embed stylesheet: Open WebUI loads /static/custom.css on every
        # page (see its app.html). Answering it ourselves lets the iframe render
        # without the chat-history sidebar, and keeps the rule in this repo
        # (chat/embed.css) rather than in a copy of Open WebUI an upgrade would
        # overwrite. A static asset, so it needs no identity.
        if self.command == "GET" and urlsplit(self.path).path == "/static/custom.css":
            self._serve_embed_css()
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
            self._send_to_studio()
            return
        headers.update(identity)

        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._tunnel(identity)
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        try:
            with self.client().stream(
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

    def _serve_embed_css(self) -> None:
        """Serve chat/embed.css as Open WebUI's /static/custom.css.

        The bytes come from this repo, not the upstream, so the embed styling
        survives Open WebUI upgrades and lives in one place shared with the
        Docker mode (which mounts the same file for Caddy).
        """
        css = (Path(__file__).parent / "embed.css").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(css)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(css)

    def _send_to_studio(self) -> None:
        """Signed out: send the whole tab to Studio, not just this frame.

        A redirect would load Studio's chat page *inside* the iframe, which
        embeds this origin again — a loop that ends as a blank frame. Breaking
        out of the frame makes the real problem (usually no session cookie here)
        visible as Studio's login page.
        """
        target = f"{self.studio_url}/login/?next=/chat/"
        body = (
            "<!doctype html><meta charset=utf-8>"
            f'<script>top.location.replace("{target}")</script>'
            f'<p>Not signed in. <a href="{target}" target="_top">Sign in to Studio</a>.</p>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _tunnel(self, identity: dict[str, str]) -> None:
        """Hand a WebSocket handshake to Open WebUI and then get out of the way.

        Nothing here understands WebSocket framing: once the upstream has agreed
        to the upgrade, the two sockets carry bytes in both directions until one
        of them closes. The identity headers go on the handshake, which is the
        only part Open WebUI authenticates. The handshake reply is checked
        first: only a 101 is piped, anything else is forwarded as a plain HTTP
        error so the browser sees a real response, not raw bytes.
        """
        upstream_url = urlsplit(chat.UPSTREAM)
        host, port = upstream_url.hostname or "127.0.0.1", upstream_url.port or 80
        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except OSError as exc:
            logger.warning("chat websocket upstream unreachable: %s", exc)
            self.send_error(502, "Chat backend unavailable")
            return

        # The handshake keeps the hop-by-hop headers this time (Connection,
        # Upgrade and the Sec-WebSocket-* set are the handshake), minus any
        # identity the client tried to supply.
        forwarded = {
            key: value for key, value in self.headers.items()
            if key.lower() not in {h.lower() for h in chat.TRUSTED_HEADERS} | {"host"}
        }
        forwarded["Host"] = f"{host}:{port}"
        forwarded.update(identity)
        request = f"GET {self.path} HTTP/1.1\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in forwarded.items()
        ) + "\r\n"

        self.close_connection = True
        client = self.connection
        try:
            upstream.sendall(request.encode("latin-1"))
            # Read the handshake reply before piping anything. The upgrade is
            # only a WebSocket once the upstream answers 101; any other status
            # (a 401 when trusted-header auth is not applied to the Socket.IO
            # path, a 500, ...) is an ordinary HTTP error that must be
            # forwarded as one — piping it would hand the browser raw error
            # bytes dressed up as WebSocket frames.
            header, leftover = _read_headers(upstream)
            status = _status_code(header)
            if status != 101:
                self._forward_handshake_error(header, status, leftover)
                return
            # The browser is still waiting for its own handshake reply, so the
            # 101 goes to it first. The read above may also have swallowed the
            # first frame bytes along with the headers; hand those back to the
            # pipe so nothing is lost.
            client.sendall(header)
            upstream.settimeout(None)
            client.settimeout(None)
            pump = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
            pump.start()
            _pipe(upstream, client, leftover)
            pump.join(timeout=1)
        except OSError:
            pass    # either side hung up; nothing to salvage
        finally:
            upstream.close()

    def _forward_handshake_error(self, header: bytes, status: int,
                                 body: bytes = b"") -> None:
        """The upstream refused the upgrade: answer the browser with a real
        HTTP response instead of piping the error as if it were a WebSocket.

        The upstream's own status and safe headers are forwarded when it
        answered with a well-formed response; otherwise a clean 502 stands in
        so the client always gets something it can parse. ``body`` is the part
        of the error body already read off the socket; the connection-close
        delimits it, so the upstream's Content-Length is not forwarded.
        """
        try:
            if status is None:
                self.send_error(502, "Chat backend unavailable")
                return
            self.send_response(status)
            for line in header.split(b"\r\n")[1:]:
                if not line or b":" not in line:
                    continue
                name, _, value = line.partition(b":")
                if name.strip().lower() in _STRIP_FROM_RESPONSE:
                    continue
                self.send_header(name.decode("latin-1"), value.strip().decode("latin-1"))
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except OSError:
            pass    # the browser already went away

    def _identify(self, cookie: str) -> dict[str, str] | None:
        """Who is this browser? None when signed out. Cached for IDENTITY_TTL."""
        hit, cached = _cached_identity(cookie)
        if hit:
            return cached
        try:
            response = self.client().get(
                f"{self.studio_url}/chat/authz",
                headers={"Cookie": cookie} if cookie else {},
            )
        except httpx.HTTPError as exc:
            # Not cached: Studio being briefly unreachable should not sign
            # everyone out for the next few seconds.
            logger.warning("chat authz unreachable: %s", exc)
            return None
        identity = (
            {h: response.headers[h] for h in chat.TRUSTED_HEADERS if h in response.headers}
            if response.status_code == 200 else None
        )
        _remember_identity(cookie, identity)
        return identity

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _proxy


def _read_headers(upstream: socket.socket) -> tuple[bytes, bytes]:
    """Read the upstream's handshake reply up to the first blank line.

    Returns (header_bytes, leftover): the leftover is anything read past the
    ``\\r\\r\\n`` terminator (the first frame bytes, when the upstream sent
    them in the same packet) and must be replayed before the pipe starts.
    """
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = upstream.recv(65536)
        if not chunk:
            break
        buffer += chunk
    index = buffer.find(b"\r\n\r\n")
    if index < 0:
        return buffer, b""
    return buffer[: index + 4], buffer[index + 4:]


def _status_code(header: bytes) -> int | None:
    """The status code of a response head, or None when it is not a response."""
    line = header.split(b"\r\n", 1)[0]
    parts = line.split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def _pipe(source: socket.socket, destination: socket.socket,
          initial: bytes = b"") -> None:
    """Copy bytes one way until the source closes, then half-close the other end.

    ``initial`` is data already read off the source (the bytes that followed
    the handshake headers) and goes out first, so nothing is lost.
    """
    try:
        if initial:
            destination.sendall(initial)
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            destination.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve(studio_port: int) -> ThreadingHTTPServer:
    """Start the proxy on chat.PROXY_PORT in a daemon thread."""
    _Handler.studio_url = f"http://127.0.0.1:{studio_port}"
    # One pool for every request; the clients that borrow it are per request.
    _Handler.transport = httpx.HTTPTransport()
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
        # Nothing here serves Ollama, and Open WebUI polls it on every page load
        # (a 500 per poll in the console) and shows an empty section in settings.
        "ENABLE_OLLAMA_API": "false",
        "WEBUI_URL": chat.public_url(),
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
