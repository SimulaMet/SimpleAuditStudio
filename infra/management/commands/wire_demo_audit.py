"""Wire the seeded Support Refund Assistant to a live demo audit."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Wire the seeded agent, Open WebUI target, scenarios, and audit run."

    def add_arguments(self, parser):
        parser.add_argument("--project", type=int, default=None)

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        from accounts.models import Project
        from infra.demo_audit import setup_demo_audit

        project = (
            Project.objects.filter(id=options["project"]).first()
            if options["project"]
            else Project.objects.order_by("id").first()
        )
        user = get_user_model().objects.order_by("id").first()
        if project is None or user is None:
            self.stderr.write(self.style.ERROR("A project and user are required."))
            return

        def log(message, *args):
            self.stdout.write(message % args if args else message)

        status = setup_demo_audit(project, user, log=log)
        self.stdout.write(self.style.SUCCESS(f"Demo audit wiring: {status}"))
