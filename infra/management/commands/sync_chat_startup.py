"""Sync Studio's chat state to Open WebUI: connections, functions, agents.

    python manage.py sync_chat_startup

The same sync the worker runs at startup, for the web boot command and for runs
where no worker starts. Idempotent; never fails the boot.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Push connections, bundled functions, OTLP credentials and agents to chat (startup sync)."

    def handle(self, *args, **options):
        from chat.sync import startup_sync

        for line in startup_sync():
            self.stdout.write(line)
