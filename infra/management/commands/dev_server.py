"""Run the local dev web server + Hatchet worker together in one process.

Combines the two daily-dev commands into one so you can start everything with a
single line:

    uv run manage.py dev_server

Equivalent to running, in parallel:
    uv run manage.py runserver                 # web/API server
    uv run manage.py run_worker --pool cpu     # audit execution worker

The web server runs in a child process (with auto-reload on by default, like
`manage.py runserver`); the worker runs in the main thread (required so its
signal handlers receive Ctrl+C). Press Ctrl+C once to stop both cleanly.
Pass --no-reload to disable auto-reload (web server then runs in a thread).

Unlike the `uvx simpleaudit-studio` CLI (which embeds its own Hatchet + SQLite
+ mock model for the end-user demo), this command uses your real `.env` config —
typically the "partial Docker" setup where Postgres + Hatchet run via
`docker compose up -d postgres hatchet-server`. If Hatchet isn't reachable the
worker retries on startup (see infra.worker.start_worker).
"""
from __future__ import annotations

import os
import sys
import threading
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


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

    def handle(self, *args, **options):
        # Zero-Docker mode implies SQLite for the domain DB too. SIMPLEAUDIT_LOCAL_SQLITE
        # must be set before Django loads settings, so if --embedded is on but the env
        # var isn't, re-exec this command once with it set. The child sees the flag
        # already satisfied and proceeds without re-execing.
        if options["embedded"] and not os.environ.get("SIMPLEAUDIT_LOCAL_SQLITE"):
            os.environ["SIMPLEAUDIT_LOCAL_SQLITE"] = "1"
            os.execv(sys.executable, [sys.executable] + sys.argv)

        from django.conf import settings

        if options["pool"]:
            settings.WORKER_POOL = options["pool"]

        # Zero-Docker mode: spin up embedded Hatchet (sidecar + embedded Postgres)
        # and point the worker's shared client at it. Setting _CLIENT directly
        # avoids touching infra.worker.get_client() (which is gated on
        # SIMPLEAUDIT_MINIMAL, a settings-load-time flag we can't flip here).
        if options["embedded"] and not options["no_worker"]:
            from infra import worker as _worker_mod
            from infra.minimal_config import start_embedded_hatchet

            print("\n📦 Starting embedded Hatchet engine (zero-Docker mode)...")
            _worker_mod._CLIENT = start_embedded_hatchet()
            print("✅ Embedded Hatchet ready — no external Postgres/Hatchet needed.\n")

        # --- Web server in a separate process ---
        # Auto-reload is on by default (like `manage.py runserver`). Django's
        # reloader installs signal handlers, which only work in a process's main
        # thread — so we can't run it in a thread here. Instead we spawn a child
        # process that runs `runserver` normally; this process keeps the worker
        # in its main thread (required for the worker's Ctrl+C handling).
        if not options["no_reload"]:
            import subprocess

            cmd = [sys.executable, sys.argv[0], "runserver", f"0.0.0.0:{options['port']}"]
            self.stdout.write(self.style.NOTICE(f"Starting web server at http://localhost:{options['port']} (auto-reload on) ..."))
            web_proc = subprocess.Popen(cmd)
            time.sleep(2)  # give the reloader + server a moment to bind
        else:
            addr = f"0.0.0.0:{options['port']}"
            self.stdout.write(self.style.NOTICE(f"Starting web server at http://localhost:{options['port']} (auto-reload off) ..."))
            web_proc = None
            web_thread = threading.Thread(
                target=lambda: call_command("runserver", addr, use_reloader=False),
                daemon=True,
            )
            web_thread.start()
            time.sleep(1)  # give the web server a moment to bind

        username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
        self.stdout.write(self.style.SUCCESS(
            f"\n🚀 Dev server running — Web UI: http://localhost:{options['port']}  "
            f"(login: {username})\n"
            f"   API docs: http://localhost:{options['port']}/api/schema/\n"
            f"   Press Ctrl+C to stop.\n"
        ))

        if options["no_worker"]:
            # Block forever on the main thread; stop the web server child (if any)
            # or let the daemon web thread die with us on Ctrl+C.
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            finally:
                if web_proc is not None:
                    web_proc.terminate()
                    web_proc.wait(timeout=5)
            return

        # --- Worker in the MAIN thread (required for signal handlers) ---
        self.stdout.write(self.style.NOTICE(f"Starting audit worker (pool={settings.WORKER_POOL})..."))
        from infra.worker import start_worker

        try:
            start_worker(max_startup_retries=60, startup_retry_delay=2.0)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nShutting down..."))
        finally:
            if web_proc is not None:
                web_proc.terminate()
                web_proc.wait(timeout=5)
