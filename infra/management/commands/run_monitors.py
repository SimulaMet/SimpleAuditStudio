"""Launch every due recurring audit (Monitor) once, then exit.

Usage:
    python manage.py run_monitors

The worker already ticks monitors every sweeper pass. This command is for
deployments where the worker may sleep (e.g. a free Hugging Face Space): point
an external cron (GitHub Actions, systemd timer) at it so ticks still fire.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Launch all due recurring audits once."

    def handle(self, *args, **options):
        from audits.monitors import run_due_monitors

        run_ids = run_due_monitors()
        if run_ids:
            self.stdout.write(self.style.SUCCESS(f"Launched {len(run_ids)} run(s): {run_ids}"))
        else:
            self.stdout.write("No monitors due.")
