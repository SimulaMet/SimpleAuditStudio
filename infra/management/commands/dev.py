"""``manage.py dev`` — the named entry point for local development.

This is the honest name for "run the embedded, hot-reloading dev stack":
SQLite + embedded Hatchet + (by default) chat, reading credentials from the
repo's ``.env``. It exists so the four run modes each have one command, and so
``dev`` and ``embedded`` (``uvx``) are clearly distinct:

    manage.py dev           # this: source checkout, auto-reload, DEBUG, .env
    uvx simpleaudit-studio  # the installed artifact, no reload, DEBUG off

``dev`` is a thin alias of ``dev_server --embedded`` with the mode pinned. It
accepts ``--disable-chat`` so chat (on by default in dev) can always be turned
off — as can be done by ``SIMPLEAUDIT_CHAT=off`` in the environment. On start
it prints a one-time sign-in link and opens the browser signed in (skip the
pop-up with ``--no-browser``).
"""
from __future__ import annotations

import os

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the local dev stack (embedded, hot-reload): web + worker + chat."

    def add_arguments(self, parser):
        parser.add_argument(
            "--port", type=int, default=int(os.environ.get("PORT", "8000")),
            help="Web server port (default: 8000, or $PORT).",
        )
        parser.add_argument(
            "--disable-chat", "--no-chat", dest="disable_chat", action="store_true",
            help="Do not run the bundled chat (Open WebUI); /chat/ stays unavailable. "
                 "Equivalent to SIMPLEAUDIT_CHAT=off.",
        )
        parser.add_argument(
            "--no-worker", action="store_true",
            help="Start only the web server (skip the worker).",
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
        # Chat on/off (flag > env > default) is decided in manage.py before
        # django.setup(), because chat.config.ENABLED freezes at import time.
        os.environ.setdefault("SIMPLEAUDIT_MODE", "dev")
        os.environ.setdefault("SIMPLEAUDIT_LOCAL_SQLITE", "1")

        # Delegate to dev_server (embedded). call_command passes options as
        # kwargs, which dev_server.handle reads by key.
        call_command(
            "dev_server",
            port=options["port"],
            pool=None,
            no_worker=options["no_worker"],
            embedded=True,
            no_reload=options["no_reload"],
            no_browser=options["no_browser"],
        )
