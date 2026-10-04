"""The front door in front of Open WebUI in embedded mode.

Two implementations, one behaviour. ``serve()`` prefers the bundled Caddy
binary (caddyserver wheel, all platforms): it generates a Caddyfile that
mirrors the docker deployment's deploy/openwebui-subpath/Caddyfile.subpath
(site-level identity strip, /chat/static/custom.css -> /chat/css-mask,
in-handle forward-auth on /chat/*, Studio for everything else) on the one
public port. If the wheel's binary cannot be located or Caddy cannot come up,
serve() falls back to the pure-Python handler in this module, which routes
the same way: /chat/* to the subpath Open WebUI, everything else to Studio's
internal web server. Either way the front door does the same three things
for /chat/*:

  1. strip any client-supplied trusted header (otherwise anyone could forge one),
  2. ask Studio ``GET /chat/authz`` who the browser is, forwarding its cookies,
  3. forward the request to Open WebUI with the returned headers added.

The Python handler additionally: serves Studio's branded favicons and the
session-aware embed stylesheet, and tunnels WebSocket upgrades — the handshake
is forwarded with the identity headers attached, and once the upstream answers
101 the two sockets are simply piped together. Open WebUI's Socket.IO then
behaves as it does behind Caddy, rather than falling back to long-polling and
logging failed upgrades.
"""
from __future__ import annotations

import atexit
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
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

# The ?__studio_admin=1 marker rides on the initial document request; the
# page's own /static/custom.css request has no query, so remember "this
# browser asked for the admin sheet" for a short window, keyed on the cookie
# (same identity the proxy already uses). Short on purpose, like IDENTITY_TTL.
ADMIN_MARKER_TTL = float(os.environ.get("SIMPLEAUDIT_CHAT_ADMIN_MARKER_TTL", "30"))
_admin_marker: dict[str, float] = {}
_admin_marker_lock = threading.Lock()


def _mark_admin(cookie: str) -> None:
    if ADMIN_MARKER_TTL <= 0:
        return
    with _admin_marker_lock:
        if len(_admin_marker) > 1024:
            _admin_marker.clear()
        _admin_marker[cookie] = time.monotonic() + ADMIN_MARKER_TTL


def _is_admin(cookie: str) -> bool:
    if ADMIN_MARKER_TTL <= 0:
        return False
    with _admin_marker_lock:
        expiry = _admin_marker.get(cookie)
        if expiry is None:
            return False
        if expiry < time.monotonic():
            _admin_marker.pop(cookie, None)
            return False
        return True

# Connection-level headers that must not be forwarded (RFC 9110 §7.6.1).
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})
_STRIP_FROM_REQUEST = _HOP_BY_HOP | {h.lower() for h in chat.TRUSTED_HEADERS}
# Dropped from the response so the iframe in Studio is allowed to render it.
# X-Frame-Options has no origin allow-list, so it can only be removed.
# Cache headers are stripped too: Open WebUI sends "cache-control: no-cache"
# but that still permits heuristic freshness, so the browser cached the
# workspace document (and the ?__studio_admin=1 document with it) across
# reloads. A cached document never reaches the proxy, so the admin marker is
# never set and the resource iframes get the chat stylesheet — re-exposing the
# workspace tab bar. We re-issue cache headers for HTML below.
_STRIP_FROM_RESPONSE = _HOP_BY_HOP | {"x-frame-options", "cache-control", "etag", "last-modified"}

# Open WebUI references its favicon with absolute paths from its own static
# directory. Serve Studio's branding at those paths so the embedded origin
# keeps the same favicon as the Studio shell. Keys are the absolute paths the
# subpath build's index.html requests (/chat/favicon.*, /chat/static/favicon*);
# its build only ships favicon.{ico,png,svg}, so the size variants are
# intercepted here too — they would otherwise 404 (the upstream's HTML error
# page). (custom.css is not here — it is Studio's embed sheet, served by the
# css_mask branch.)
_BRANDED_ASSETS = {
    "/chat/favicon.svg": ("logo.svg", "image/svg+xml"),
    "/chat/favicon.png": ("logo.png", "image/png"),
    "/chat/favicon.ico": ("logo.svg", "image/svg+xml"),
    "/chat/static/favicon.svg": ("logo.svg", "image/svg+xml"),
    "/chat/static/favicon-16x16.svg": ("logo.svg", "image/svg+xml"),
    "/chat/static/favicon-32x32.svg": ("logo.svg", "image/svg+xml"),
    "/chat/static/favicon.png": ("logo.png", "image/png"),
    "/chat/static/favicon-16x16.png": ("logo.png", "image/png"),
    "/chat/static/favicon-32x32.png": ("logo.png", "image/png"),
    "/chat/static/favicon-96x96.png": ("logo.png", "image/png"),
    "/chat/static/apple-touch-icon.png": ("logo.png", "image/png"),
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    studio_url = "http://127.0.0.1:8001"     # set by serve() (internal port)
    public_origin = "http://localhost:8000"  # set by serve() (browser-visible)
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
        path = urlsplit(self.path).path
        subpath = chat.SUBPATH

        # Studio branding for the favicons OWUI's html references — the
        # subpath build requests them under /chat/static/.
        branded_asset = _BRANDED_ASSETS.get(path)
        if self.command == "GET" and branded_asset:
            self._serve_branded_asset(*branded_asset)
            return

        # Studio's embed stylesheet: the subpath build loads
        # /chat/static/custom.css on every page (see its app.html). Answering
        # it ourselves lets the iframe render without the chat-history
        # sidebar, and keeps the rule in this repo (chat/embed.css) rather
        # than in a copy of Open WebUI an upgrade would overwrite. A static
        # asset, so it needs no identity.
        if self.command == "GET" and path == f"{subpath}/static/custom.css":
            self._serve_embed_css(admin=_is_admin(self.headers.get("Cookie", "")))
            return

        if path != subpath and not path.startswith(subpath + "/"):
            # Not chat: everything else is Studio's own site. Forward it as-is
            # to the internal web server — its middleware does the login
            # redirect, and the request never touches Open WebUI.
            self._proxy_to_studio()
            return

        # From here it is chat: identify the browser, then forward.
        cookie = self.headers.get("Cookie", "")
        is_admin_doc = "__studio_admin=1" in urlsplit(self.path).query
        if is_admin_doc:
            _mark_admin(cookie)

        identity = self._identify(cookie)
        if identity is None:
            self._send_to_studio()
            return

        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._tunnel(identity)
            return

        self._forward_upstream(chat_upstream_base(), identity=identity,
                               admin_doc=is_admin_doc)

    def _proxy_to_studio(self) -> None:
        """Not chat: forward as-is to Studio's internal web server.

        Studio authenticates with the browser's own session cookie, so no
        identity headers are added; the client's headers go through verbatim
        (minus the ones a client may never set).
        """
        self._forward_upstream(self.studio_url)

    def _forward_upstream(self, base: str, identity: dict[str, str] | None = None,
                          admin_doc: bool = False) -> None:
        """Stream ``base + self.path`` from the upstream back to the browser."""
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _STRIP_FROM_REQUEST}
        # The body is forwarded byte for byte, so the upstream may only use an
        # encoding this client asked for. Without this httpx adds its own
        # Accept-Encoding and the client gets gzip it cannot read.
        if not any(k.lower() == "accept-encoding" for k in headers):
            headers["Accept-Encoding"] = "identity"
        if admin_doc:
            # The Studio mask is injected into these documents as text, so the
            # upstream must answer identity-encoded — with a browser
            # Accept-Encoding, iter_raw() would yield compressed bytes that
            # the injection step (and its UTF-8 round-trip) would corrupt.
            # The document is a small SPA shell, so the extra bytes are noise.
            headers["Accept-Encoding"] = "identity"
        if identity:
            headers.update(identity)

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        try:
            with self.client().stream(
                self.command, base + self.path, headers=headers, content=body,
            ) as upstream:
                self.send_response(upstream.status_code)
                # Whether the body below is modified (Studio mask injected),
                # which makes the upstream Content-Length wrong for the wire.
                will_inject = admin_doc and "text/html" in upstream.headers.get("Content-Type", "").lower()
                for key, value in upstream.headers.multi_items():
                    key_l = key.lower()
                    if key_l not in _STRIP_FROM_RESPONSE:
                        if will_inject and key_l == "content-length":
                            continue
                        self.send_header(key, value)
                # HTML documents must not be served from the browser cache: a
                # cached workspace document never reaches this handler, so a
                # ?__studio_admin=1 load would skip the marker and the iframe
                # would fall back to the chat stylesheet. The document is a
                # Svelte SPA shell (assets are content-hashed), so no-store is
                # cheap here. Non-HTML (API/JSON) gets no-cache: back/forward
                # cache stays usable, but stale entries are never honored.
                html = "text/html" in upstream.headers.get("Content-Type", "").lower()
                if html:
                    self.send_header("Cache-Control", "no-store")
                else:
                    self.send_header("Cache-Control", "no-cache")
                # Responses are streamed without a known length (SSE included),
                # so the connection delimits the body.
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()

                if html and admin_doc:
                    # Buffer the (small, content-hashed SPA shell) so Studio's
                    # mask is injected inline; the external /static/custom.css
                    # link cannot be relied on to carry it.
                    doc = b"".join(upstream.iter_raw()).decode("utf-8", "replace")
                    inject = self._admin_inline()
                    marker = "</head>" if "</head>" in doc else "</body>"
                    if marker in doc and inject:
                        doc = doc.replace(marker, inject + marker, 1)
                    payload = doc.encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    return
                for chunk in upstream.iter_raw():
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except httpx.HTTPError as exc:
            logger.warning("chat upstream error: %s", exc)
            self.send_error(502, "Chat backend unavailable")
        except (BrokenPipeError, ConnectionResetError):
            pass    # the browser navigated away mid-stream

    def _admin_inline(self) -> str:
        """Inline <style>/<script> block injected into marked admin documents.

        Reading both files from disk here (not at import time) means edits to
        chat/embed_admin.css / chat/embed_admin.js take effect on the next
        page load without restarting the proxy, which StatReloader does not
        manage. The CSS is inlined too — Open WebUI's /static/custom.css link
        can be answered from a stale browser-cache entry or race the marker,
        so the document carries its own mask that always applies.
        """
        embed_dir = os.path.dirname(__file__)
        try:
            with open(os.path.join(embed_dir, "embed_admin.css"), "r", encoding="utf-8") as f:
                css = f.read()
            with open(os.path.join(embed_dir, "embed_admin.js"), "r", encoding="utf-8") as f:
                js = f.read()
        except OSError:
            return ""
        return (
            "\n<!-- Studio admin embed mask (injected by SimpleAudit chat proxy) -->\n"
            "<style>\n" + css + "\n</style>\n"
            "<script>\n" + js + "\n</script>\n"
        )

    def _serve_embed_css(self, admin: bool = False) -> None:
        """Serve Studio's embed stylesheet as Open WebUI's /static/custom.css.

        ``admin`` selects chat/embed_admin.css (workspace mask) over
        chat/embed.css (chat mask). The bytes come from this repo, not the
        upstream, so the embed styling survives Open WebUI upgrades and lives
        in one place shared with the Docker mode (which mounts the same file
        for Caddy).
        """
        filename = "embed_admin.css" if admin else "embed.css"
        css = (Path(__file__).parent / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(css)))
        # no-store, not just no-cache: the response body depends on the
        # per-browser admin marker state, and this URL is also fetched by the
        # chat iframe. "no-cache" with a Date header still lets browsers apply
        # heuristic freshness and serve the other mask from cache, which
        # re-exposes the workspace tab bar in the resource iframes.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(css)

    def _serve_branded_asset(self, filename: str, content_type: str) -> None:
        """Serve Studio branding for Open WebUI's absolute favicon URLs."""
        asset = (Path(__file__).resolve().parents[1] / "static" / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(asset)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(asset)

    def _send_to_studio(self) -> None:
        """Signed out: send the whole tab to Studio, not just this frame.

        A redirect would load Studio's chat page *inside* the iframe, which
        embeds this origin again — a loop that ends as a blank frame. Breaking
        out of the frame makes the real problem (usually no session cookie here)
        visible as Studio's login page.
        """
        # The browser sees the public origin (the front door's port), never
        # the internal one. /ai/ is Studio's wrapper page.
        target = f"{self.public_origin}/login/?next=/ai/"
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
        upstream_url = urlsplit(chat_upstream_base())
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


# --- Caddy front door (embedded mode) ---------------------------------------
# The bundled Caddy binary (caddyserver wheel) does the forward-auth on
# chat.PUBLIC_PORT using a Caddyfile that mirrors the docker deployment's
# (deploy/openwebui-subpath/Caddyfile.subpath) site-for-site. It is preferred
# over the pure-Python handler because it is the same proxy the shipped
# compose uses. The wheel ships macOS/Linux/Windows binaries, so serve() only
# falls back to the Python handler on an unusual install layout without one.
_caddy_process: subprocess.Popen | None = None
_caddy_log_file: object | None = None
_caddy_lock = threading.Lock()


def _caddy_binary() -> str | None:
    """Locate the bundled Caddy binary, or None to use the Python fallback.

    The ``caddyserver`` wheel ships platform binaries for macOS (arm64/
    x86_64), Linux (glibc/musl, arm64/x86_64) and Windows (amd64/arm64); its
    ``get_caddy_executable()`` resolves the real Mach-O/PE/ELF binary across
    venv, pipx and uvx layouts. A ``shutil.which("caddy")`` first pass keeps
    working for machines that also have a system Caddy on PATH.
    """
    found = shutil.which("caddy")
    if found:
        return found
    try:
        import caddyserver
        cand = Path(caddyserver.get_caddy_executable())
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    except Exception:    # missing wheel / source-tree install -> fallback
        logger.debug("caddyserver binary probe failed", exc_info=True)
    return None


def _caddy_static_dir() -> Path:
    """Studio's static/ (the branded favicon assets Caddy serves)."""
    return Path(__file__).resolve().parents[1] / "static"


def chat_upstream_base() -> str:
    """chat.UPSTREAM without its path — the host:port part.

    UPSTREAM carries the subpath (the build serves at /chat), but requests
    arrive already carrying it, so the forwarding base is host:port only and
    the path goes on as-is (the same no-strip rule as the Caddyfile).
    """
    parts = urlsplit(chat.UPSTREAM)
    return f"{parts.scheme}://{parts.hostname or '127.0.0.1'}:{parts.port or 8080}"


def _caddyfile(port: int, internal_port: int | None = None) -> str:
    """The generated front-door Caddyfile — a faithful mirror of
    deploy/openwebui-subpath/Caddyfile.subpath, with the service hostnames
    swapped for loopback addresses:

      {$STUDIO_PORT:8000}           -> ``port``        (the one public port)
      {$STUDIO_UPSTREAM:web:8000}   -> 127.0.0.1:``internal_port``  (Studio)
      open-webui:8080               -> host:port of chat.UPSTREAM   (Open WebUI)
      {$STUDIO_URL}                 -> the public origin (signed-out 302)

    Same routing as the proven file: /chat/* (NO prefix strip — the subpath
    build serves its socket at the full path /chat/ws/socket.io) goes through
    the in-handle forward-auth to Studio's /chat/authz and then to Open WebUI;
    /chat/static/custom.css and /chat/static/loader.js are intercepted first
    and rewritten to /chat/css-mask and /chat/loader-mask; everything else
    falls through to Studio.

    Auth lives INSIDE the /chat/* handle block on purpose: Caddy sorts
    top-level routes by directive order, so a top-level auth would run after
    the terminal handle and never apply (see the proven file's comments).
    """
    internal = internal_port if internal_port is not None else chat.INTERNAL_PORT
    upstream = urlsplit(chat.UPSTREAM)
    owui = f"{upstream.hostname or '127.0.0.1'}:{upstream.port or 8080}"
    studio = f"127.0.0.1:{internal}"
    login_origin = f"http://localhost:{port}"
    lines = [
        "{",
        "\tauto_https off",
        "\tadmin off",
        "}",
        "",
        f":{port} {{",
        "\t# Never let a client supply its own identity.",
        "\trequest_header -X-Studio-Email",
        "\trequest_header -X-Studio-Name",
        "\trequest_header -X-Studio-Role",
        "",
        # Studio's /ai/ iframe points at the bare subpath with query params
        # (/chat?models=...). `handle /chat/*` does NOT match a bare /chat,
        # so send it to /chat/ (query preserved) before route selection.
        "\trewrite /chat /chat/",
        "",
        "\t# Studio's embed stylesheet: session-aware admin mask (css_mask).",
        "\thandle /chat/static/custom.css {",
        f"\t\treverse_proxy {studio} {{",
        "\t\t\trewrite /chat/css-mask",
        "\t\t}",
        "\t}",
        # Studio's embed mask script: the subpath build's otherwise-empty
        # loader.js (the SPA's first <script>) becomes chat/embed_admin.js
        # (loader_mask). The script self-gates on the page URL carrying
        # embed=admin, so only the admin workspace iframes are masked.
        "\thandle /chat/static/loader.js {",
        f"\t\treverse_proxy {studio} {{",
        "\t\t\trewrite /chat/loader-mask",
        "\t\t}",
        "\t}",
        "",
        # Studio branding for the favicons OWUI's html references — the
        # subpath build requests them under /chat/static/ (route is absolute).
    ]
    for route, (fname, _ct) in _BRANDED_ASSETS.items():
        lines += [
            f"\thandle {route} {{",
            f"\t\troot * {_caddy_static_dir()}",
            f"\t\trewrite * /{fname}",
            "\t\tfile_server",
            "\t}",
        ]
    lines += [
        "",
        "\t# Subpath Open WebUI: no prefix strip — the request path arrives",
        "\t# already carrying /chat, which is what the build expects.",
        "\t# Auth lives INSIDE this handle (subroute handlers run in file order,",
        "\t# so auth precedes the proxy) — the same fix that made the subpath",
        "\t# build work: a top-level auth directive loses the ordering race.",
        "\thandle /chat/* {",
        f"\t\treverse_proxy {studio} {{",
        "\t\t\tmethod GET",
        "\t\t\trewrite /chat/authz",
        "\t\t\theader_up X-Forwarded-Method {method}",
        "\t\t\theader_up X-Forwarded-Uri {uri}",
        "\t\t\t@good status 2xx",
        "\t\t\thandle_response @good {",
        "\t\t\t\trequest_header X-Studio-Email {rp.header.X-Studio-Email}",
        "\t\t\t\trequest_header X-Studio-Name {rp.header.X-Studio-Name}",
        "\t\t\t\trequest_header X-Studio-Role {rp.header.X-Studio-Role}",
        "\t\t\t}",
        "\t\t\t@signedout status 401",
        "\t\t\thandle_response @signedout {",
        f"\t\t\t\tredir {login_origin}/login/ 302",
        "\t\t\t}",
        "\t\t}",
        f"\t\treverse_proxy {owui} {{",
        "\t\t\t# The iframe must be allowed to render it (OWUI sets it).",
        "\t\t\theader_down -X-Frame-Options",
        "\t\t}",
        "\t}",
        "",
        "\t# Everything else -> Studio (Django). The subpath fork never emits",
        "\t# root-relative refs, so nothing from OWUI falls through here.",
        "\thandle {",
        f"\t\treverse_proxy {studio}",
        "\t}",
        "}",
    ]
    return "\n".join(lines) + "\n"


def caddy_config_path() -> Path:
    return home_dir() / "Caddyfile"


def caddy_pid_file() -> Path:
    return home_dir() / "caddy.pid"


def _stop_caddy_stale() -> None:
    """Stop a Caddy left behind by a hard-killed run (holds the public port).

    Mirrors _stop_stale for Open WebUI: only a leftover (orphaned) process is
    touched. Best-effort — any error is logged and swallowed.
    """
    from infra.minimal_config import _orphaned, _parent_pid, _pid_alive

    file = caddy_pid_file()
    try:
        if not file.exists():
            return
        raw = file.read_text().strip()
        pid = int(raw) if raw.isdigit() else 0
        if pid and _pid_alive(pid):
            parent = _parent_pid(pid)
            if parent is not None and not _orphaned(parent):
                return
            logger.warning("Stopping orphaned Caddy (PID %d) left by a killed run", pid)
            _terminate(pid)
        file.unlink(missing_ok=True)
    except Exception:    # cleanup must never block startup
        logger.warning("Could not clean up a leftover Caddy", exc_info=True)


def _caddy_ready(port: int, timeout: float = 15.0) -> bool:
    """Poll the front door until it answers any HTTP status, or time runs out."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2.0)
            return True
        except urllib.error.HTTPError:
            return True    # any HTTP response = the listener is up
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def _start_caddy(port: int, internal_port: int) -> subprocess.Popen | None:
    """Start the bundled Caddy front door on the public port, or None if it
    cannot.

    ``port`` is the one port the browser sees (chat.PUBLIC_PORT);
    ``internal_port`` is where Studio's web server moved while the front door
    is up. Returns the process on success. Raises RuntimeError if caddy
    starts but never becomes ready, so serve() can fall back to the Python
    handler.
    """
    from infra.minimal_config import _pid_alive

    global _caddy_log_file
    binary = _caddy_binary()
    if binary is None:
        return None
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    _stop_caddy_stale()

    config = caddy_config_path()
    config.write_text(_caddyfile(port, internal_port))

    _log_file = home_dir() / "caddy.log"
    log = _log_file.open("a")
    _caddy_log_file = log
    log.write(f"\n--- caddy start (studio :{internal_port}) ---\n")
    log.flush()
    process = subprocess.Popen(
        [binary, "run", "--config", str(config), "--adapter", "caddyfile"],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    if not _caddy_ready(port):
        log.close()
        _caddy_log_file = None
        _terminate(process.pid)
        raise RuntimeError(
            f"Caddy started (PID {process.pid}) but never bound :{port}"
        )
    # Never overwrite a file holding a *live* caddy from another run.
    try:
        raw = caddy_pid_file().read_text().strip()
        if raw.isdigit() and _pid_alive(int(raw)):
            logger.warning(
                "Caddy (PID %s) already running; leaving its pid file as-is", raw
            )
        else:
            caddy_pid_file().write_text(str(process.pid))
    except OSError:
        caddy_pid_file().write_text(str(process.pid))
    logger.info("Chat front door: bundled Caddy on :%d (PID %d)",
                port, process.pid)
    return process


def stop_caddy() -> None:
    """Stop the Caddy this process started. Safe to call more than once."""
    global _caddy_process, _caddy_log_file
    with _caddy_lock:
        process, _caddy_process = _caddy_process, None
        if process is not None and process.poll() is None:
            _terminate(process.pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Caddy (PID %d) did not exit", process.pid)
        if _caddy_log_file is not None:
            try:
                _caddy_log_file.close()
            except OSError:
                pass
            _caddy_log_file = None
        if process is not None:
            file = caddy_pid_file()
            try:
                if file.read_text().strip() == str(process.pid):
                    file.unlink(missing_ok=True)
            except OSError:
                pass


def serve(internal_port: int):
    """Start the front door on the one public port (chat.PUBLIC_PORT).

    ``internal_port`` is where Studio's web server listens while the front
    door is up. Prefers the bundled Caddy binary (caddyserver); falls back to
    the pure-Python handler when no binary is available (e.g. Windows) or
    Caddy cannot come up. Returns the server/process, or None on fallback
    failure (logged).
    """
    global _caddy_process
    _caddy_binary_path = _caddy_binary()
    if _caddy_binary_path is not None:
        try:
            with _caddy_lock:
                if _caddy_process is None or _caddy_process.poll() is not None:
                    _caddy_process = _start_caddy(chat.PUBLIC_PORT, internal_port)
            return _caddy_process
        except Exception:    # a broken binary must not kill the chat
            logger.warning("Caddy front door failed; using the Python proxy",
                           exc_info=True)
    # Fallback: the pure-Python front door (no caddy needed).
    _Handler.studio_url = f"http://127.0.0.1:{internal_port}"
    _Handler.public_origin = f"http://localhost:{chat.PUBLIC_PORT}"
    # One pool for every request; the clients that borrow it are per request.
    _Handler.transport = httpx.HTTPTransport()
    server = ThreadingHTTPServer(("0.0.0.0", chat.PUBLIC_PORT), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Chat front door: Python proxy on :%d (no caddy binary)",
                chat.PUBLIC_PORT)
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
    """Stop the Open WebUI this process started. Safe to call more than once.

    Also stops the Caddy front door, so the callers' single stop call on the
    way out tears down everything this run started.
    """
    global _process, _log_file
    stop_caddy()
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
            # Only remove a file that records *this* process: a failed start
            # may have found the file already holding another run's live
            # Open WebUI, and deleting it would orphan that one from the
            # next run's cleanup.
            file = pid_file()
            try:
                if file.read_text().strip() == str(process.pid):
                    file.unlink(missing_ok=True)
            except OSError:
                pass


# Best-effort clean shutdown even if the caller forgets to stop explicitly.
# (Registered after stop_open_webui, so atexit runs it FIRST: caddy, then
# Open WebUI — the reverse of startup order.)
atexit.register(stop_caddy)
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
                # The health URL is fixed, so a 200 can come from *another*
                # Open WebUI already on this port (ours then died on the bind).
                # Only trust it while our own process is still alive.
                if response.status == 200 and process.poll() is None:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2.0)
    return False


def start_open_webui(studio_port: int | None = None) -> subprocess.Popen:
    """Start the subpath Open WebUI bound to loopback, in trusted-header mode.

    Launched like the docker image: CLI-uvicorn of open_webui.main:app with
    FROM_INIT_PY + WEBUI_SUBPATH (see _spawn). The interpreter comes from the
    managed venv (or SIMPLEAUDIT_CHAT_OWUI_VENV / SIMPLEAUDIT_CHAT_CMD).

    ``studio_port`` is where Studio's own web server listens; it is only used to
    point Open WebUI's OTLP exporter at Studio's listener when OTLP is enabled.

    One instance per process: a second call returns the running one. Leftovers
    from a hard-killed previous run are stopped first.
    """
    with _process_lock:
        if _process is not None and _process.poll() is None:
            return _process

        home = home_dir()
        home.mkdir(parents=True, exist_ok=True)
        _stop_stale()
        return _spawn(home, studio_port)


OWUI_WHEEL_URL = os.environ.get(
    "SIMPLEAUDIT_CHAT_WHEEL",
    "https://github.com/SushantGautam/open-webui/releases/download/"
    "v0.11.4-subpath/open_webui-0.11.4-py3-none-any.whl",
)
OWUI_PACKAGE = os.environ.get("SIMPLEAUDIT_CHAT_PACKAGE", "open-webui")


def _owui_venv_dir() -> Path:
    """Where the managed venv holding the subpath Open WebUI wheel lives."""
    return home_dir().parent / "openwebui-venv"


def _probe_interpreter(py: str) -> bool:
    """True when this interpreter can import the subpath Open WebUI wheel."""
    try:
        result = subprocess.run(
            [py, "-c", ("import open_webui, importlib.metadata as m; "
                        "m.version('open_webui')")],
            capture_output=True, timeout=30, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _locate_owui_interpreter(home: Path) -> str:
    """Find (or create) the interpreter that runs the subpath Open WebUI wheel.

    Resolution order:
      1. ``SIMPLEAUDIT_CHAT_OWUI_VENV`` — an existing venv (``bin/python``)
      2. the Studio venv itself, if the wheel happens to be installed there
      3. the managed venv at ``<data>/openwebui-venv`` — created once (via
         ``uv venv`` + wheel install) and reused on later runs
      4. a ``uv tool`` environment for the pinned wheel, if one already exists

    Nothing here downloads silently unless a step actually needs to: the
    managed-venv step only runs when no other interpreter has the wheel, and
    it is what makes the first run work from a bare checkout.
    """
    override = os.environ.get("SIMPLEAUDIT_CHAT_OWUI_VENV")
    if override:
        py = Path(override) / "bin" / "python"
        if py.exists() and _probe_interpreter(str(py)):
            return str(py)
        raise RuntimeError(
            f"SIMPLEAUDIT_CHAT_OWUI_VENV={override} does not contain the "
            "Open WebUI subpath wheel (import open_webui failed)."
        )

    current = sys.executable
    if _probe_interpreter(current):
        return current

    managed = _owui_venv_dir()
    managed_py = managed / "bin" / "python"
    if managed_py.exists():
        if _probe_interpreter(str(managed_py)):
            return str(managed_py)
        logger.warning("Stale Open WebUI venv at %s; rebuilding it", managed)
        shutil.rmtree(managed, ignore_errors=True)

    uv = shutil.which("uv")
    if uv:
        try:
            # --seed: include pip in the venv (uv venv omits it by default,
            # and the wheel install below invokes `python -m pip`).
            subprocess.run([uv, "venv", str(managed), "--python", "3.11", "--seed"],
                           check=True, capture_output=True, timeout=120)
            subprocess.run(
                [managed_py, "-m", "pip", "install", OWUI_WHEEL_URL],
                check=True, capture_output=True, timeout=900,
            )
            if _probe_interpreter(str(managed_py)):
                return str(managed_py)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                "Could not install the Open WebUI subpath wheel into "
                f"{managed} ({exc}). Check the network, or set "
                "SIMPLEAUDIT_CHAT_OWUI_VENV / SIMPLEAUDIT_CHAT_CMD."
            ) from exc

    # Last resort: an existing uv-tool environment for the pinned wheel.
    tools_root = Path.home() / ".local" / "share" / "uv" / "tools"
    if tools_root.is_dir():
        for env_dir in tools_root.glob(f"{OWUI_PACKAGE}*/bin"):
            py = env_dir / "python"
            if py.exists() and _probe_interpreter(str(py)):
                return str(py)

    raise RuntimeError(
        "No Open WebUI subpath wheel found. It is not a Studio dependency, so "
        "either: install it into a venv (uv venv && pip install "
        f"{OWUI_WHEEL_URL}) and set SIMPLEAUDIT_CHAT_OWUI_VENV, or set "
        "SIMPLEAUDIT_CHAT_CMD to the command that starts it."
    )


def _secret_key(home: Path) -> str:
    """Open WebUI's signing key — generated once, persisted.

    A direct uvicorn launch has no `serve` to persist the key, and a fresh
    random key on every start would sign every session out; the file plays
    the role ``.webui_secret_key`` serves for `open-webui serve`.
    """
    key_file = home / "webui_secret_key"
    try:
        if key_file.exists():
            value = key_file.read_text().strip()
            if value:
                return value
        value = secrets.token_urlsafe(32)
        key_file.write_text(value)
        os.chmod(key_file, 0o600)
        return value
    except OSError as exc:
        # A non-persistable key still works for this run.
        logger.warning("Could not persist the Open WebUI secret key: %s", exc)
        return secrets.token_urlsafe(32)


def _spawn(home: Path, studio_port: int | None = None) -> subprocess.Popen:
    """Build the command and environment, and start the server.

    The launch is the one the Docker image uses (verified E2E against it):
    CLI-uvicorn of ``open_webui.main:app`` with ``FROM_INIT_PY=true`` (so the
    frontend build is found inside the wheel) and ``WEBUI_SUBPATH=/chat``.
    ``open-webui serve`` is deliberately NOT used: its uvicorn-level
    ``root_path`` double-prefixes the subpath build's /chat mount.
    """
    global _process, _log_file

    from infra.minimal_config import _pid_alive

    base = chat_upstream_base()
    host = urlsplit(base).hostname or "127.0.0.1"
    port = urlsplit(base).port or 8080

    command = os.environ.get("SIMPLEAUDIT_CHAT_CMD")
    if command:
        argv = command.split()
    else:
        py = _locate_owui_interpreter(home)
        argv = [py, "-m", "uvicorn", "open_webui.main:app",
                "--host", host, "--port", str(port),
                "--forwarded-allow-ips", "*"]

    env = {
        **os.environ,
        "DATA_DIR": str(home),
        # The subpath build: routes mount at /chat, and FROM_INIT_PY makes
        # the wheel's packaged frontend/ the build dir (the image does the
        # same through its start.sh / ENV).
        "WEBUI_SUBPATH": chat.SUBPATH,
        "FROM_INIT_PY": "true",
        "WEBUI_SECRET_KEY": _secret_key(home),
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": chat.EMAIL_HEADER,
        "WEBUI_AUTH_TRUSTED_NAME_HEADER": chat.NAME_HEADER,
        "WEBUI_AUTH_TRUSTED_ROLE_HEADER": chat.ROLE_HEADER,
        "ENABLE_SIGNUP": "false",
        # Nothing here serves Ollama, and Open WebUI polls it on every page load
        # (a 500 per poll in the console) and shows an empty section in settings.
        "ENABLE_OLLAMA_API": "false",
        "WEBUI_URL": chat.public_url(),
    }
    if chat.OTLP_ENABLED:
        # Export Open WebUI's spans to Studio's own OTLP listener. Open WebUI
        # picks the HTTP exporter from OTEL_OTLP_SPAN_EXPORTER (not the standard
        # OTEL_EXPORTER_OTLP_PROTOCOL) and passes the endpoint to the exporter
        # explicitly, so the exporter POSTs to it as-is (protobuf wire format):
        # this must be the full /otlp/v1/traces URL. Exports unauthenticated by
        # default (matches the listener's "none" credential fallback); for a
        # basic/bearer credential, set OTEL_BASIC_AUTH_USERNAME / _PASSWORD in the
        # environment, which is inherited here.
        env.update(
            ENABLE_OTEL="true",
            ENABLE_OTEL_TRACES="true",
            OTEL_OTLP_SPAN_EXPORTER="http",
            OTEL_EXPORTER_OTLP_ENDPOINT=chat.otlp_endpoint_url(studio_port),
            OTEL_SERVICE_NAME=chat.OTLP_SERVICE_NAME,
        )
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
    # Never overwrite a file that records a *live* Open WebUI: that one
    # belongs to another run, and trampling it would orphan it from the
    # next run's cleanup. A dead PID is a stale leftover — safe to replace.
    file = pid_file()
    try:
        raw = file.read_text().strip()
        if raw.isdigit() and _pid_alive(int(raw)):
            logger.warning(
                "Open WebUI (PID %s) is already running; recording our PID %d "
                "would orphan it, so the pid file is left as-is",
                raw, _process.pid,
            )
        else:
            file.write_text(str(_process.pid))
    except OSError:
        file.write_text(str(_process.pid))
    return _process
