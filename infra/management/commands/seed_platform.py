"""Idempotent first-run seed: scenario packs + model connections + demo audit runs.

Run automatically by the minimal-config CLI and docker-compose entrypoint, or manually:
    python manage.py seed_platform [--project 1] [--packs safety rag health system_prompt]

Seeds in order:
  1. Scenario packs (from simpleaudit package)
  2. Default model connections
  3. Demo audit runs (from pre-recorded fixture, no API key needed)

Safe to run multiple times — skips anything already present.
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from infra.seed import DEFAULT_PACKS, import_scenario_pack, seed_default_model_connections

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Seed scenario packs and default model connections (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--project", type=int, default=1,
            help="Project ID to seed (default: 1)",
        )
        parser.add_argument(
            "--packs", nargs="+", default=DEFAULT_PACKS,
            help=f"Scenario packs to import (default: {' '.join(DEFAULT_PACKS)})",
        )
        parser.add_argument(
            "--skip-models", action="store_true",
            help="Skip creating default model connections",
        )
        parser.add_argument(
            "--skip-packs", action="store_true",
            help="Skip importing scenario packs",
        )
        parser.add_argument(
            "--skip-demo-audits", action="store_true",
            help="Skip seeding demo audit runs from fixture",
        )

    def handle(self, *args, **options):
        from accounts.models import Project
        from django.contrib.auth import get_user_model

        User = get_user_model()

        project_id = options["project"]
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            raise CommandError(
                f"Project {project_id} not found. Run bootstrap_platform first."
            )

        user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        if not user:
            raise CommandError("No user found. Run bootstrap_platform first.")

        self.stdout.write(f"Seeding project '{project.name}' (id={project_id}) as {user.username}")

        if not options["skip_packs"]:
            self._seed_scenario_packs(project, user, options["packs"])

        if not options["skip_models"]:
            for message in seed_default_model_connections(project, user):
                self.stdout.write(f"  {message}")

        if not options["skip_demo_audits"]:
            self._seed_demo_audits(project, user)

        self.stdout.write(self.style.SUCCESS("Seed complete."))

    def _seed_demo_audits(self, project, user) -> None:
        """Seed demo audit runs from pre-recorded fixture (idempotent)."""
        from django.core.management import call_command
        try:
            call_command("seed_demo_audits", project=project.id, verbosity=0)
        except CommandError as e:
            self.stderr.write(self.style.WARNING(f"Demo audit seed skipped: {e}"))

    def _seed_scenario_packs(self, project, user, packs: list[str]):
        try:
            from simpleaudit import list_scenario_packs
        except ImportError:
            self.stderr.write(
                self.style.WARNING(
                    "simpleaudit package not installed — skipping scenario pack import."
                )
            )
            return

        available = set(list_scenario_packs())
        for pack_name in packs:
            if pack_name not in available:
                self.stderr.write(
                    self.style.WARNING(f"Pack '{pack_name}' not available. Skipping.")
                )
                continue

            set_name = f"SimpleAudit: {pack_name}"
            _, version, status = import_scenario_pack(project, user, pack_name)
            if status == "skipped":
                self.stdout.write(f"  ⏭ {set_name} already exists — skipping.")
            elif status == "empty":
                self.stdout.write(f"  ⏭ {set_name}: pack is empty — skipping.")
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ {set_name}: v{version.version}, {version.scenario_count} scenarios"
                    )
                )
