"""The front door in front of Open WebUI in embedded mode.

``serve()`` runs the bundled Caddy binary (caddyserver wheel, all platforms)
with a generated Caddyfile that mirrors the docker deployment's
deploy/openwebui-subpath/Caddyfile.subpath site-for-site (site-level
identity strip, /chat/static/custom.css -> /chat/css-mask, in-handle
forward-auth on /chat/*, Studio for everything else) on the one public port.
Caddy is the only implementation of this front door: it does the three
things it must for /chat/* (strip any client-supplied trusted header, ask
Studio ``GET /chat/authz`` who the browser is, forward to Open WebUI with
the returned headers added), plus branded favicons, the embed stylesheet
mask, and WebSocket upgrade forwarding. A missing binary means a broken
install (the wheel is a hard dependency), so ``serve()`` fails loudly rather
than degrading to a second, divergent implementation.
"""
from __future__ import annotations

import atexit
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from chat import config as chat

logger = logging.getLogger(__name__)


# Open WebUI is managed like the embedded Hatchet engine: one instance per
# process, stopped on the way out (atexit as well as the CLI's own shutdown),
# and a run that was hard-killed has its leftovers cleaned up by the next start.
_process: subprocess.Popen | None = None
_log_file = None
_process_lock = threading.Lock()


# --- Caddy front door (embedded mode) ---------------------------------------
# The bundled Caddy binary (caddyserver wheel) does the forward-auth on
# chat.PUBLIC_PORT using a Caddyfile that mirrors the docker deployment's
# (deploy/openwebui-subpath/Caddyfile.subpath) site-for-site — the same proxy
# the shipped compose uses.
#
# Open WebUI references its favicon with absolute paths from its own static
# directory. The generated Caddyfile serves Studio's branding at those paths
# so the embedded origin keeps the same favicon as the Studio shell. Keys are
# the absolute paths the subpath build's index.html requests
# (/chat/favicon.*, /chat/static/favicon*); its build only ships
# favicon.{ico,png,svg}, so the size variants are mapped too — they would
# otherwise 404 (the upstream's HTML error page). (custom.css is not here —
# it is Studio's embed sheet, served by the css-mask branch.)
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
_caddy_process: subprocess.Popen | None = None
_caddy_log_file: object | None = None
_caddy_lock = threading.Lock()


def _caddy_binary() -> str:
    """Locate the bundled Caddy binary, or fail.

    The ``caddyserver`` wheel is a hard dependency and ships platform
    binaries for macOS (arm64/x86_64), Linux (glibc/musl, arm64/x86_64) and
    Windows (amd64/arm64); ``get_caddy_executable()`` resolves the real
    Mach-O/PE/ELF binary across venv, pipx and uvx layouts. A
    ``shutil.which("caddy")`` first pass keeps working for machines that also
    have a system Caddy on PATH. A missing binary means a broken install, so
    the front door (and thus the chat module) cannot start.
    """
    found = shutil.which("caddy")
    if found:
        return found
    try:
        import caddyserver
        cand = Path(caddyserver.get_caddy_executable())
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    except Exception:    # missing wheel / source-tree install
        logger.debug("caddyserver binary probe failed", exc_info=True)
    raise RuntimeError(
        "Caddy binary not found: the 'caddyserver' package (a hard "
        "dependency) does not provide one on this platform. Reinstall the "
        "environment (e.g. `uv sync` / `uv pip install caddyserver`) and "
        "start again — the chat front door requires it."
    )


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
        # Studio's /playground/ iframe points at the bare subpath with query params
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
        # The authz call is a rewritten GET, so the page's ?embed=admin
        # query never reaches Studio (rewrite /chat/authz drops the query
        # context). Pass the embed flag as a header instead — authz stamps
        # the session admin from it, which is what makes /chat/css-mask
        # serve the admin skin (tab-bar mask) and /chat/loader-mask the
        # directory-row mask. Empty unless this page carries it.
        "\t\t\theader_up X-Studio-Embed {uri.query.embed}",
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
        # The subpath fork's KnowledgeBase "Back" button navigates to the
        # root-relative /workspace/knowledge (a missed ${base} prefix in the
        # fork build) — a hard GET that would 404 on Studio. Repair it here
        # at the front door: 308 the browser onto the subpath, which
        # re-enters the /chat/* handle below (forward-auth + OWUI) and lands
        # on the collection list. A 308 (not an internal rewrite) because the
        # SPA is built with base=/chat and its client router reload-loops on
        # an out-of-base URL — the bar must show /chat/....
        # The redir target MUST lead with a placeholder: Caddy's caddyfile
        # parser misreads a leading '/' as a matcher (the status word then
        # becomes the Location, and adapt still reports success). So the
        # subpath is spelled {env.WEBUI_SUBPATH}{subpath} — the env
        # placeholder expands empty because _start_caddy spawns Caddy with
        # WEBUI_SUBPATH="". {http.request.uri} preserves the query string.
        # Dead code once the fork is rebuilt with the ${base} prefix.
        "\thandle /workspace/* {",
        f"\t\tredir {{env.WEBUI_SUBPATH}}{chat.SUBPATH}{{http.request.uri}} 308",
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


def _start_caddy(port: int, internal_port: int) -> subprocess.Popen:
    """Start the bundled Caddy front door on the public port.

    ``port`` is the one port the browser sees (chat.PUBLIC_PORT);
    ``internal_port`` is where Studio's web server moved while the front door
    is up. Returns the process on success. Raises RuntimeError if caddy
    starts but never becomes ready, or ``_caddy_binary()`` cannot find the
    binary at all.
    """
    from infra.minimal_config import _pid_alive

    global _caddy_log_file
    binary = _caddy_binary()
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
    # The Caddyfile's workspace back-nav repair spells its redirect target as
    # {env.WEBUI_SUBPATH}{subpath} (a leading placeholder — see _caddyfile).
    # The subpath itself comes from chat.SUBPATH, so the env placeholder must
    # expand to nothing: spawn Caddy with WEBUI_SUBPATH cleared so a stray
    # value in our environment can't prepend a second path segment.
    caddy_env = {**os.environ, "WEBUI_SUBPATH": ""}
    from simpleaudit_studio.process_guard import guarded_command

    process = subprocess.Popen(
        guarded_command([binary, "run", "--config", str(config), "--adapter", "caddyfile"]),
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        env=caddy_env,
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


def serve(internal_port: int) -> subprocess.Popen:
    """Start the Caddy front door on the one public port (chat.PUBLIC_PORT).

    ``internal_port`` is where Studio's web server listens while the front
    door is up. Returns the Caddy process. Raises RuntimeError when the
    bundled binary is missing (a broken install — see ``_caddy_binary()``)
    or Caddy cannot come up: there is no second front-door implementation to
    fall back to, and a degraded proxy would silently diverge from the
    Caddyfile it is meant to mirror.
    """
    global _caddy_process
    with _caddy_lock:
        if _caddy_process is None or _caddy_process.poll() is not None:
            _caddy_process = _start_caddy(chat.PUBLIC_PORT, internal_port)
        return _caddy_process


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


def _wheel_marker(managed: Path) -> Path:
    """Records which wheel URL built the managed venv (written at build time)."""
    return managed / ".studio-wheel-url"


def _managed_venv_matches_wheel(managed: Path) -> bool:
    """True when the managed venv was built from the currently pinned wheel.

    Without this, a venv built from an older release of the same package name
    (and thus importable) would be reused forever, silently pinned to the old
    frontend. A missing marker (venv predating this check) also counts as a
    mismatch, so every existing machine rebuilds exactly once.
    """
    try:
        return _wheel_marker(managed).read_text().strip() == OWUI_WHEEL_URL
    except OSError:
        return False


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
        if _probe_interpreter(str(managed_py)) and _managed_venv_matches_wheel(managed):
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
                _wheel_marker(managed).write_text(OWUI_WHEEL_URL)
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
    from simpleaudit_studio.process_guard import guarded_command

    _process = subprocess.Popen(
        guarded_command(argv), env=env, cwd=str(home),
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
