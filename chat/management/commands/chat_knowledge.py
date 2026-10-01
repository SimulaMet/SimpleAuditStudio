"""List the Open WebUI knowledge bases Studio can read, or the files in one.

    python manage.py chat_knowledge              # every knowledge base
    python manage.py chat_knowledge --id <id>    # one, with its files

This is the consuming direction: what Open WebUI holds, in plain dicts Studio
code can build on (see chat/api.py).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from chat import config as chat
from chat.api import ChatAPI, ChatAPIError


class Command(BaseCommand):
    help = "List Open WebUI knowledge bases."

    def add_arguments(self, parser):
        parser.add_argument("--id", help="Show one knowledge base and the files in it")
        parser.add_argument("--as-user", help="Username to read as; default is a superuser")

    def handle(self, *args, **options):
        if not chat.ENABLED:
            raise CommandError("Chat is disabled. Set SIMPLEAUDIT_CHAT=embedded or docker.")

        api = ChatAPI.as_user(_user(options["as_user"]))
        try:
            if options["id"]:
                base = api.knowledge_base(options["id"])
                self.stdout.write(f"{base['name']} — {base['description']}")
                for file in base["files"]:
                    self.stdout.write(f"  {file['name']}  ({file['id']})")
                return
            bases = api.knowledge_bases()
        except ChatAPIError as exc:
            raise CommandError(str(exc)) from exc

        if not bases:
            self.stdout.write(self.style.WARNING("No knowledge bases (or none shared with this user)."))
            return
        for base in bases:
            files = "" if base["file_count"] is None else f"  {base['file_count']} file(s)"
            self.stdout.write(f"{base['id']}  {base['name']}{files}")


def _user(username: str | None):
    from accounts.models import User

    if username:
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"No user named '{username}'.")
        return user
    user = User.objects.filter(is_superuser=True).order_by("id").first()
    if user is None:
        raise CommandError("No superuser to read as; pass --as-user.")
    return user
