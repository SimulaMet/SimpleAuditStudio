"""``manage.py mode`` — print exactly which run mode is active.

One command answers "what am I actually running?" when behavior looks wrong:
the resolved mode, its process/DB/queue/chat defaults, the effective chat
setting, and — when Django settings are available — the concrete database
backend and path.

    uv run manage.py mode
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Show the resolved run mode and its settings."

    def handle(self, *args, **options):
        from config import runtime

        try:
            profile = runtime.resolve_mode()
        except runtime.UnknownModeError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            self.stderr.write(
                "Run under a mode to inspect one, e.g.:\n"
                "    SIMPLEAUDIT_MODE=dev manage.py mode\n"
                "    uv run manage.py dev --no-worker --no-reload  # then mode"
            )
            return

        lines = [
            f"Mode:      {profile.id}",
            f"Process:   {profile.process}"
            + (" (hot-reload)" if profile.id == "dev" else ""),
            f"Database:  {self._describe_db()}",
            f"Queue:     {profile.queue}",
            f"Chat:      {self._describe_chat(profile)}",
            f"DEBUG:     {'on' if profile.debug else 'off'}",
        ]
        data_dir = os.environ.get("SIMPLEAUDIT_DATA_DIR")
        if profile.id in {"embedded", "single-docker"} and data_dir:
            lines.append(f"Data dir:  {os.path.expanduser(data_dir)}")

        for line in lines:
            self.stdout.write(line)

    def _describe_db(self) -> str:
        from django.conf import settings

        default = settings.DATABASES.get("default", {})
        name = default.get("NAME", "?")
        if "sqlite" in default.get("ENGINE", ""):
            return f"sqlite ({os.path.expanduser(str(name))})"
        host = default.get("HOST", "")
        port = default.get("PORT", "")
        return f"postgres ({name} @ {host}:{port})"

    def _describe_chat(self, profile) -> str:
        from config import runtime

        effective = runtime.effective_chat_mode(profile.id)
        if effective == "off":
            return "off (SIMPLEAUDIT_CHAT=off)"
        if effective == profile.chat_mode and profile.chat == "on":
            return f"on (default: {effective})"
        return f"on ({effective})"
