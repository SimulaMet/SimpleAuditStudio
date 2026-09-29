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
from chat.api import ChatAPI, ChatAPIError, connection_payload


class Command(BaseCommand):
    help = "Push Studio model connections into Open WebUI."

    def add_arguments(self, parser):
        parser.add_argument("--project", help="Workspace slug; default is every workspace")
        parser.add_argument("--dry-run", action="store_true", help="Only show what would be pushed")

    def handle(self, *args, **options):
        if not chat.ENABLED:
            raise CommandError("Chat is disabled. Set SIMPLEAUDIT_CHAT=embedded or docker.")

        from model_registry.models import ModelConnection

        connections = ModelConnection.objects.filter(enabled=True)
        if options["project"]:
            connections = connections.filter(project__slug=options["project"])
            if not connections.exists():
                raise CommandError(f"No enabled connections in workspace '{options['project']}'.")

        payloads = [connection_payload(conn) for conn in connections]
        payloads = [payload for payload in payloads if payload["base_url"]]
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
            result = ChatAPI.as_user(_admin()).push_connections(payloads)
        except ChatAPIError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Pushed {result['pushed']} connection(s); kept {result['kept']} added in Open WebUI."
        ))


def _admin():
    """A Studio user Open WebUI will treat as an admin — provider config needs one."""
    from accounts.models import User

    user = User.objects.filter(is_superuser=True).order_by("id").first()
    if user is None:
        raise CommandError("No superuser to act as; Open WebUI's provider config needs an admin.")
    return user
