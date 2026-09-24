"""Idempotent first-run bootstrap for admin user and default project."""
import os

from django.core.management.base import BaseCommand, CommandError

from accounts.services import bootstrap_admin_and_default_project
from infra.startup_checks import validate_startup_environment


class Command(BaseCommand):
    help = "Create initial admin user and default project if missing."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "studio"))
        parser.add_argument("--email", default=os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.local"))
        parser.add_argument("--password", default=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", ""))
        parser.add_argument("--project-name", default=os.environ.get("BOOTSTRAP_PROJECT_NAME", "Default"))

    def handle(self, *args, **options):
        password = options["password"]
        errors = validate_startup_environment(bootstrap_password=password)
        if errors:
            for error in errors:
                self.stderr.write(self.style.ERROR(error))
            raise CommandError("Refusing to bootstrap with unsafe configuration.")
        if not password:
            raise CommandError(
                "Bootstrap password is required. Set BOOTSTRAP_ADMIN_PASSWORD or pass --password."
            )
        user, project = bootstrap_admin_and_default_project(
            username=options["username"],
            email=options["email"],
            password=password,
            project_name=options["project_name"],
        )
        self.stdout.write(self.style.SUCCESS(f"Bootstrap complete: user={user.username} project={project.slug}"))
