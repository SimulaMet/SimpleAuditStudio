"""Seed the default agentic audit scenario pack (D01-D10).

This is the only agentic pack. ``infra.fixtures.agentic_scenarios`` owns it; this
command is the entry point the first-run seed and the embedded launcher call.
Idempotent: a set that already has a published version is left alone.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Seed the default agentic audit scenario pack (D01-D10)."

    def add_arguments(self, parser):
        parser.add_argument("--project", type=int, default=None, help="Project id (default: the first project).")
        parser.add_argument("--reseed", action="store_true", help="Publish a new version even if one exists.")

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        from accounts.models import Project
        from infra.fixtures.agentic_scenarios import seed_agentic_scenarios

        project = (
            Project.objects.filter(pk=options["project"]).first()
            if options["project"] else Project.objects.order_by("id").first()
        )
        user = get_user_model().objects.filter(is_superuser=True).order_by("id").first()
        if project is None or user is None:
            raise CommandError("Project and superuser are required; run bootstrap_platform first.")
        scenario_set, version, status = seed_agentic_scenarios(
            project, user, log=self.stdout.write, reseed=options["reseed"]
        )
        self.stdout.write(f"{status}: {scenario_set.name} v{version.version}")
