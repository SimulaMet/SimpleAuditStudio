"""One-shot local development setup: migrate + bootstrap admin + seed data.

Chains the three idempotent first-run steps into a single command so local
setup is one line instead of three:

    uv run manage.py setup_local

Equivalent to running, in order:
    manage.py migrate
    manage.py bootstrap_platform
    manage.py seed_platform

Safe to re-run — every step is idempotent and skips what already exists.
Requires BOOTSTRAP_PASSWORD (or --password) for the admin user, matching
bootstrap_platform's safety checks.
"""
from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command


class Command(BaseCommand):
    help = "Local dev setup in one step: migrate + bootstrap admin + seed data."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=os.environ.get("BOOTSTRAP_USERNAME", "studio"))
        parser.add_argument("--email", default=os.environ.get("BOOTSTRAP_EMAIL", "admin@example.local"))
        parser.add_argument("--password", default=os.environ.get("BOOTSTRAP_PASSWORD", ""))
        parser.add_argument("--project-name", default=os.environ.get("BOOTSTRAP_PROJECT_NAME", "Default"))
        parser.add_argument(
            "--skip-seed", action="store_true",
            help="Skip seeding scenario packs / model connections (migrate + bootstrap only).",
        )

    def handle(self, *args, **options):
        password = options["password"]
        if not password:
            raise CommandError(
                "Bootstrap password is required. Set BOOTSTRAP_PASSWORD or pass --password."
            )

        self.stdout.write("→ Applying migrations...")
        call_command("migrate", verbosity=0, interactive=False)

        self.stdout.write("→ Bootstrapping admin user + default project...")
        call_command(
            "bootstrap_platform",
            username=options["username"],
            email=options["email"],
            password=options["password"],
            project_name=options["project_name"],
            verbosity=1,
        )

        if not options["skip_seed"]:
            self.stdout.write("→ Seeding scenario packs + model connections...")
            call_command("seed_platform", verbosity=1)

        self.stdout.write(self.style.SUCCESS(
            "\nSetup complete. Start the server with:\n"
            "    uv run manage.py runserver\n"
            "Then open http://localhost:8000 (login: %s / your password)." % options["username"]
        ))
