"""Run the local dev web server + Hatchet worker together in one process.

This is the zero-Docker development stack: SQLite + an embedded Hatchet engine
+ (by default) the bundled chat, all in one process with a hot-reloading web
server. The named entry point for it is ``manage.py dev``; ``dev_server
--embedded`` is the same thing spelled out.

    uv run manage.py dev                 # web + worker + embedded queue + chat
    uv run manage.py dev --disable-chat  # same, without Open WebUI

On start it applies migrations and bootstraps the admin user (a superuser) and
default workspace from BOOTSTRAP_* in `.env`, like the Compose web service and
the uvx CLI do, so a local database never lags behind the code.

The web server runs in a child process (with auto-reload on by default, like
`manage.py runserver`); the worker runs in the main thread (required so its
signal handlers receive Ctrl+C). Press Ctrl+C once to stop both cleanly.
Pass --no-reload to disable auto-reload (web server then runs in a thread).

`dev_server` used to also connect to external Postgres + Hatchet from `.env`
when `--embedded` was omitted; that legacy path is removed. `--embedded` (or an
explicit SIMPLEAUDIT_MODE) is required.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


def _open_browser_when_ready(url: str, port: int, timeout: float = 30.0) -> None:
    """Wait until the web server answers, then open `url` in the default browser.

    `url` is the one-time /auto-login/?token=... link (see auto_login_view).
    Readiness is polled on /healthz (unauthenticated) so the single-use token
    is not consumed by the probe. Runs in a daemon thread; any failure just
    prints a hint. Mirrors the uvx CLI's behavior.
    """
    import requests

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"http://localhost:{port}/healthz", timeout=2).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.5)
    else:
        print(f"⚠️  Web UI did not come up within {timeout:.0f}s — open {url} manually.")
        return
    import webbrowser

    if not webbrowser.open(url):
        print(f"⚠️  Could not open a browser automatically — visit {url} manually.")


def _sync_chat_models() -> str:
    """Give the freshly-started chat Studio's model connections, and say how it went.

    Signals keep the chat in step afterwards (chat/signals.py); this is the first
    push, for a chat that has just started or was off while connections changed.
    Without it, Open WebUI keeps whatever provider config it had before (stale
    keys, deleted connections), and a chat pinned to a connection whose key was
    not pushed answers "Not authenticated" until the next connection save.
    """
    from chat.api import ChatAPIError
    from chat.sync import push_now

    try:
        result = push_now()
    except ChatAPIError as exc:
        return f"Models not synced to chat: {exc}"
    kept = f", kept {result['kept']} added in chat" if result["kept"] else ""
    return f"Synced {result['pushed']} model connection(s) to chat{kept}."


class Command(BaseCommand):
    help = "Run the local dev web server + Hatchet worker together."

    def add_arguments(self, parser):
        parser.add_argument(
            "--port", type=int, default=int(os.environ.get("PORT", "8000")),
            help="Web server port (default: 8000, or $PORT).",
        )
        parser.add_argument(
            "--pool", default=None,
            help="Worker pool label (cpu/gpu). Defaults to WORKER_POOL env.",
        )
        parser.add_argument(
            "--no-worker", action="store_true",
            help="Start only the web server (skip the worker).",
        )
        parser.add_argument(
            "--embedded", action="store_true",
            help="Zero-Docker mode: start an embedded Hatchet engine (sidecar binary "
                 "+ embedded Postgres) instead of connecting to external services. "
                 "First run downloads ~53MB and takes ~15s; afterwards it's fast. "
                 "Pair with SIMPLEAUDIT_LOCAL_SQLITE=1 for a fully self-contained setup.",
        )
        parser.add_argument(
            "--no-reload", action="store_true",
            help="Disable the web server's auto-reloader (enabled by default).",
        )
        parser.add_argument(
            "--no-browser", action="store_true",
            help="Do not open the default browser signed in; the one-time "
                 "sign-in link is still printed.",
        )

    def handle(self, *args, **options):
        # The legacy non-embedded path (connect to external Postgres + Hatchet
        # from .env) is removed: dev_server is a single-process, zero-Docker
        # command. It needs --embedded, or an explicit single-process mode.
        explicit_mode = (os.environ.get("SIMPLEAUDIT_MODE") or "").strip().lower()
        if not options["embedded"] and explicit_mode:
            # An explicit mode must be one dev_server can actually run
            # (single-process); unknown and container modes get clear errors.
            from config.runtime import SINGLE_PROCESS_MODES, VALID_MODES

            if explicit_mode not in VALID_MODES:
                raise CommandError(
                    f"SIMPLEAUDIT_MODE={explicit_mode!r} is not a valid mode. "
                    "Valid modes: " + ", ".join(VALID_MODES) + "."
                )
            if explicit_mode not in SINGLE_PROCESS_MODES:
                raise CommandError(
                    f"SIMPLEAUDIT_MODE={explicit_mode!r} is a container mode; "
                    "dev_server is single-process. Use `docker compose up` / "
                    "`docker run` for it, or run `manage.py dev` for local "
                    "development."
                )
        elif not options["embedded"]:
            from config.runtime import SINGLE_PROCESS_MODES

            raise CommandError(
                "dev_server now requires zero-Docker mode. Use:\n"
                "    manage.py dev                 (embedded, hot-reload, chat)\n"
                "or set SIMPLEAUDIT_MODE to one of: "
                + ", ".join(SINGLE_PROCESS_MODES)
                + ".\n(Container modes run via docker: see `docs/deployment.md`.)"
            )
        os.environ.setdefault("SIMPLEAUDIT_MODE", "dev")

        # Whether the embedded Hatchet engine runs (a single-process embedded
        # mode with a worker): --embedded, or SIMPLEAUDIT_MODE in {dev, embedded}.
        use_embedded = options["embedded"] or explicit_mode in {"dev", "embedded"}

        # Zero-Docker mode implies SQLite for the domain DB too. manage.py sets
        # SIMPLEAUDIT_LOCAL_SQLITE before settings load; if the command was
        # started another way without it, re-exec once with it set.
        if not os.environ.get("SIMPLEAUDIT_LOCAL_SQLITE"):
            os.environ["SIMPLEAUDIT_LOCAL_SQLITE"] = "1"
            os.execv(sys.executable, [sys.executable] + sys.argv)

        from django.conf import settings

        self.prepare_database()

        if options["pool"]:
            settings.WORKER_POOL = options["pool"]

        # Chat is on by default in the single-process modes. manage.py has
        # already frozen SIMPLEAUDIT_CHAT (flag > env > default) before
        # django.setup(); this setdefault is a belt-and-braces fallback for
        # any path that reached here without it set.
        from config import runtime

        os.environ.setdefault(
            "SIMPLEAUDIT_CHAT",
            runtime.effective_chat_mode(os.environ.get("SIMPLEAUDIT_MODE", "dev")),
        )

        # With chat on, the front door (Caddy) owns the public port and the
        # web server binds the internal port instead; Caddy proxies the rest
        # of the site to it. Same topology as the docker deployment.
        from chat import config as _chat_config

        web_port = _chat_config.INTERNAL_PORT if _chat_config.ENABLED else options["port"]

        # Optional chat (Open WebUI + its front door). Only starts when
        # SIMPLEAUDIT_CHAT is enabled; a failed start never blocks the rest of
        # the stack, mirroring the uvx CLI.
        chat_process = self.start_chat_if_enabled(options["port"], web_port)

        # Zero-Docker mode: spin up embedded Hatchet (sidecar + embedded Postgres)
        # and point the worker's shared client at it. Setting _CLIENT directly
        # avoids touching infra.worker.get_client() (which is gated on
        # SIMPLEAUDIT_MINIMAL, a settings-load-time flag we can't flip here).
        if use_embedded and not options["no_worker"]:
            from infra import worker as _worker_mod
            from infra.minimal_config import start_embedded_hatchet

            print("\n📦 Starting embedded Hatchet engine (zero-Docker mode)...")
            _worker_mod._CLIENT = start_embedded_hatchet()
            print("✅ Embedded Hatchet ready — no external Postgres/Hatchet needed.\n")

        # One-time sign-in link (like the uvx CLI): /auto-login/?token=... signs
        # the browser in as the bootstrap user, single use. The token must be in
        # the environment BEFORE the web process starts, so the runserver child
        # (which serves the endpoint) inherits it.
        auto_login_url = ""
        if use_embedded:
            import secrets

            os.environ["SIMPLEAUDIT_AUTO_LOGIN_TOKEN"] = secrets.token_urlsafe(32)
            auto_login_url = (
                f"http://localhost:{options['port']}/auto-login/?"
                f"token={os.environ['SIMPLEAUDIT_AUTO_LOGIN_TOKEN']}"
            )

        # --- Web server in a separate process ---
        # Auto-reload is on by default (like `manage.py runserver`). Django's
        # reloader installs signal handlers, which only work in a process's main
        # thread — so we can't run it in a thread here. Instead we spawn a child
        # process that runs `runserver` normally; this process keeps the worker
        # in its main thread (required for the worker's Ctrl+C handling).
        if not options["no_reload"]:
            import subprocess

            cmd = [sys.executable, sys.argv[0], "runserver", f"0.0.0.0:{web_port}"]
            self.stdout.write(self.style.NOTICE(f"Starting web server at http://localhost:{web_port} (auto-reload on) ..."))
            web_proc = subprocess.Popen(cmd)
            time.sleep(2)  # give the reloader + server a moment to bind
        else:
            addr = f"0.0.0.0:{web_port}"
            self.stdout.write(self.style.NOTICE(f"Starting web server at http://localhost:{web_port} (auto-reload off) ..."))
            web_proc = None
            web_thread = threading.Thread(
                target=lambda: call_command("runserver", addr, use_reloader=False),
                daemon=True,
            )
            web_thread.start()
            time.sleep(1)  # give the web server a moment to bind

        username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
        chat_line = (
            f"   Chat:       http://localhost:{options['port']}/ai/\n"
            if chat_process is not None
            else ""
        )
        self.stdout.write(self.style.SUCCESS(
            f"\n🚀 Dev server running — Web UI: http://localhost:{options['port']}  "
            f"(login: {username})\n"
            f"   API docs:   http://localhost:{options['port']}/api/schema/\n"
            f"{chat_line}"
            f"   Press Ctrl+C to stop.\n"
        ))
        if auto_login_url:
            self.stdout.write(self.style.SUCCESS(
                f"🔑 One-time sign-in link: {auto_login_url}\n"
                f"   (signs you in as {username}; works exactly once — "
                f"pass --no-browser to skip the browser opening.)\n"
            ))
            if not options["no_browser"]:
                threading.Thread(
                    target=_open_browser_when_ready,
                    args=(auto_login_url, options["port"]),
                    daemon=True,
                ).start()

        if options["no_worker"]:
            # Block forever on the main thread; stop the web server child (if any)
            # or let the daemon web thread die with us on Ctrl+C.
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            finally:
                self._stop_web(web_proc)
                self._stop_chat()
            return

        # --- Worker in the MAIN thread (required for signal handlers) ---
        self.stdout.write(self.style.NOTICE(f"Starting audit worker (pool={settings.WORKER_POOL})..."))
        from infra.worker import start_worker

        try:
            start_worker(max_startup_retries=60, startup_retry_delay=2.0)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nShutting down..."))
        finally:
            self._stop_web(web_proc)
            self._stop_chat()

    def start_chat_if_enabled(self, public_port: int, internal_port: int):
        """Start Open WebUI + its front door when SIMPLEAUDIT_CHAT is on.

        ``public_port`` is the one port the browser sees (the front door);
        ``internal_port`` is where the web server listens while the front door
        is up. Mirrors the uvx CLI: chat is one part of the stack, so a failed
        start (no wheel, a taken port, a failed spawn) never blocks the rest.
        Returns the Open WebUI process, or None when chat is off or failed.
        """
        from chat import config as chat_config

        if not chat_config.ENABLED:
            return None

        from chat import proxy as chat_proxy

        self.stdout.write(self.style.NOTICE("Starting chat (Open WebUI)..."))
        if chat_proxy.is_first_run():
            self.stdout.write(self.style.WARNING(
                "   First start installs the subpath wheel and can take a few minutes."
            ))
            self.stdout.write("   Studio is usable right away; /ai/ (chat) works once it is ready.")
        try:
            process = chat_proxy.start_open_webui(internal_port)
        except (OSError, RuntimeError) as exc:
            self.stdout.write(self.style.WARNING(
                f"\n⚠️  Chat could not start ({exc}); continuing without it.\n"
            ))
            return None
        chat_proxy.serve(internal_port)

        def report():
            if chat_proxy.wait_until_ready(process):
                print(f"\n✅ Chat is ready — http://localhost:{public_port}/ai/", flush=True)
                print(f"   {_sync_chat_models()}\n", flush=True)
            elif process.poll() is not None:
                print(f"\n⚠️  Chat stopped (exit {process.returncode}). Studio is unaffected.", flush=True)
                print(f"   What happened: {chat_proxy.log_path()}\n", flush=True)
            else:
                print("\n⚠️  Chat is still not answering. Studio is unaffected.", flush=True)

        threading.Thread(target=report, daemon=True).start()
        return process

    def _stop_web(self, web_proc):
        if web_proc is not None:
            web_proc.terminate()
            web_proc.wait(timeout=5)

    def _stop_chat(self):
        try:
            from chat.proxy import stop_open_webui

            stop_open_webui()
        except Exception:  # chat teardown must never block shutdown
            logger.exception("Chat teardown skipped")

    def prepare_database(self):
        """Apply migrations and make sure the bootstrap admin (a superuser) exists."""
        self.stdout.write("→ Applying migrations...")
        call_command("migrate", verbosity=0, interactive=False)
        password = os.environ.get("BOOTSTRAP_PASSWORD", "")
        if not password:
            self.stdout.write(self.style.WARNING(
                "BOOTSTRAP_PASSWORD is not set: skipping admin bootstrap (set it in .env)."
            ))
            return
        call_command(
            "bootstrap_platform",
            username=os.environ.get("BOOTSTRAP_USERNAME", "studio"),
            email=os.environ.get("BOOTSTRAP_EMAIL", "admin@example.local"),
            password=password,
            project_name=os.environ.get("BOOTSTRAP_PROJECT_NAME", "Default"),
            verbosity=0,
        )
