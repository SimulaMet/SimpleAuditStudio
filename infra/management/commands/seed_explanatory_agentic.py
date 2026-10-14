"""Seed the Explanatory Agentic Audit Scenarios pack.

Usage:
    python manage.py seed_explanatory_agentic [--project <slug>]

Creates 8 fully-populated agentic scenarios that exercise every auditable
field in the schema v2, with detailed inline documentation of what each
field does and how it maps to Open WebUI OTLP signals.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Project


class Command(BaseCommand):
    help = "Seed the explanatory agentic audit scenario pack (8 scenarios, all fields)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--project",
            type=str,
            default=None,
            help="Project slug (defaults to first project)",
        )

    def handle(self, *args, **options):
        project_slug = options["project"]
        if project_slug:
            project = Project.objects.filter(slug=project_slug).first()
            if not project:
                raise CommandError(f"Project '{project_slug}' not found.")
        else:
            project = Project.objects.order_by("id").first()
            if not project:
                raise CommandError("No projects exist. Create one first.")

        user = project.created_by or None

        self.stdout.write(f"Seeding explanatory agentic scenarios into '{project.name}'...")

        from infra.fixtures.agentic_scenarios import seed_agentic_scenarios

        scenario_set, version, status = seed_agentic_scenarios(project, user, log=self._log)

        if status == "skipped":
            self.stdout.write(self.style.WARNING(f"Already seeded: {scenario_set.name}"))
        elif status == "imported":
            self.stdout.write(self.style.SUCCESS(
                f"Created '{scenario_set.name}' v{version.version} "
                f"with {version.scenario_count} scenarios."
            ))
            self.stdout.write("\nScenarios:")
            for item in version.items.select_related("scenario"):
                self.stdout.write(f"  • {item.scenario.key}: {item.scenario.title}")
            self.stdout.write("\nNext steps:")
            self.stdout.write("  1. Ensure 'Support Refund Assistant' agent exists")
            self.stdout.write("  2. Register the simulachat.sushant.pp.ua connection")
            self.stdout.write("  3. Create an audit run targeting the agent with this set")
        else:
            self.stdout.write(f"Status: {status}")

    def _log(self, msg):
        self.stdout.write(f"  {msg}")
