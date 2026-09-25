"""Purge smoke-test / E2E artifacts so a fresh deployment starts clean.

Removes audit runs (and their durable events + scenario results) whose name or
scenario key matches a configurable prefix, plus any model endpoints / scenarios /
scenario sets created by the E2E driver. Idempotent and safe to re-run.

By default this only touches rows that look like test data (name/key starting with
the given prefixes). Pass ``--all-runs`` to remove every audit run in the project
(use with care on a shared instance).
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from audits.events import AuditEvent, ScenarioResult
from audits.models import AuditRun
from model_registry.models import ModelEndpoint
from scenarios.models import (
    Scenario,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


class Command(BaseCommand):
    help = "Purge smoke-test / E2E audit runs and their associated artifacts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-prefix",
            default="E2E Smoke Run",
            help="Only delete audit runs whose name starts with this string.",
        )
        parser.add_argument(
            "--endpoint-prefix",
            default="Mock Model (E2E",
            help="Only delete model endpoints whose display_name starts with this string.",
        )
        parser.add_argument(
            "--scenario-key-prefix",
            default="e2e-",
            help="Only delete scenarios whose key starts with this string.",
        )
        parser.add_argument(
            "--set-name-prefix",
            default="E2E Set",
            help="Only delete scenario sets whose name starts with this string.",
        )
        parser.add_argument(
            "--all-runs",
            action="store_true",
            help="Delete ALL audit runs (not just those matching --run-prefix).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry = options["dry_run"]

        # 1. Audit runs
        if options["all_runs"]:
            runs = AuditRun.objects.all()
        else:
            runs = AuditRun.objects.filter(name__startswith=options["run_prefix"])
        run_ids = list(runs.values_list("id", flat=True))

        # 2. Durable events + scenario results for those runs
        events = AuditEvent.objects.filter(run_id__in=[str(r) for r in run_ids])
        results = ScenarioResult.objects.filter(run_id__in=[str(r) for r in run_ids])

        # 3. Model endpoints
        endpoints = ModelEndpoint.objects.filter(display_name__startswith=options["endpoint_prefix"])
        endpoint_ids = list(endpoints.values_list("id", flat=True))

        # 4. Scenarios
        scenarios = Scenario.objects.filter(key__startswith=options["scenario_key_prefix"])
        scenario_ids = list(scenarios.values_list("id", flat=True))

        # 5. Scenario sets
        sets = ScenarioSet.objects.filter(name__startswith=options["set_name_prefix"])
        # Exclude sets whose versions are still pinned by a run we're keeping.
        kept_run_version_set_ids = set(
            AuditRun.objects.exclude(id__in=run_ids)
            .exclude(scenario_set_version__isnull=True)
            .values_list("scenario_set_version__scenario_set_id", flat=True)
        )
        all_set_ids = list(sets.values_list("id", flat=True))
        set_ids = [sid for sid in all_set_ids if sid not in kept_run_version_set_ids]
        sets = ScenarioSet.objects.filter(id__in=set_ids)

        self.stdout.write(f"Would {'delete' if not dry else 'report'}:")
        self.stdout.write(f"  audit runs:      {len(run_ids)}")
        self.stdout.write(f"  audit events:    {events.count()}")
        self.stdout.write(f"  scenario results:{results.count()}")
        self.stdout.write(f"  model endpoints: {len(endpoint_ids)}")
        self.stdout.write(f"  scenarios:       {len(scenario_ids)}")
        self.stdout.write(f"  scenario sets:   {len(set_ids)}")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — nothing deleted."))
            return

        # Delete children before parents to respect FK constraints.
        events.delete()
        results.delete()
        runs.delete()
        # Version items -> versions -> sets
        ScenarioSetVersionItem.objects.filter(version__scenario_set_id__in=set_ids).delete()
        ScenarioSetVersion.objects.filter(scenario_set_id__in=set_ids).delete()
        sets.delete()
        # Revisions -> scenarios
        from scenarios.models import ScenarioRevision

        ScenarioRevision.objects.filter(scenario_id__in=scenario_ids).delete()
        scenarios.delete()
        endpoints.delete()

        self.stdout.write(self.style.SUCCESS("Purge complete."))
