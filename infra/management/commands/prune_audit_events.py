"""Delete per-turn progress events of runs that finished long ago.

Usage:
    python manage.py prune_audit_events [--days N]

The worker's sweeper already does this hourly (``SIMPLEAUDIT_EVENT_RETENTION_DAYS``,
default 30, 0 disables). Run it by hand to reclaim space at once. State and
terminal events are kept, so finished runs still show their outcome.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete per-turn events (scenario_turn, scenario_rep_started) of long-finished runs."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None,
                            help="Age in days after a run finished (default: SIMPLEAUDIT_EVENT_RETENTION_DAYS or 30).")

    def handle(self, *args, **options):
        from audits.events import prune_finished_run_events
        from infra.worker import _event_retention_days

        days = options["days"] if options["days"] is not None else _event_retention_days()
        deleted = prune_finished_run_events(days)
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} event(s) of runs finished more than {days} day(s) ago."))
