"""Push Studio model connections into Open WebUI, so chat offers the same models.

    python manage.py sync_chat_models                 # every workspace's connections
    python manage.py sync_chat_models --project demo  # one workspace
    python manage.py sync_chat_models --dry-run       # show what would be pushed

Connections Studio pushed before are replaced; providers added inside Open WebUI
are left alone (see chat/api.py).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from chat import config as chat
from chat import sync
from chat.api import ChatAPIError


class Command(BaseCommand):
    help = "Push Studio model connections into Open WebUI."

    def add_arguments(self, parser):
        parser.add_argument("--project", help="Workspace slug; default is every workspace")
        parser.add_argument("--dry-run", action="store_true", help="Only show what would be pushed")

    def handle(self, *args, **options):
        if not chat.ENABLED:
            raise CommandError("Chat is disabled. Set SIMPLEAUDIT_CHAT=embedded or docker.")

        payloads = sync.connections_to_push()
        if options["project"]:
            payloads = [p for p in payloads if p["project_slug"] == options["project"]]
            if not payloads:
                raise CommandError(f"No enabled connections in workspace '{options['project']}'.")
        for payload in payloads:
            key = "key set" if payload["api_key"] else "no key"
            self.stdout.write(f"  {payload['name']}  {payload['base_url']}  ({key})")
        if not payloads:
            self.stdout.write(self.style.WARNING("No connections with a base URL to push."))
            return
        if options["dry_run"]:
            self.stdout.write(f"Would push {len(payloads)} connection(s).")
            return

        try:
            # One code path with the signals, so a manual sync and an automatic
            # one leave Open WebUI in the same state.
            result = sync.push_now(payloads)
        except ChatAPIError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Pushed {result['pushed']} connection(s); kept {result['kept']} added in Open WebUI."
        ))

