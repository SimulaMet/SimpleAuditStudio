"""AuditEvent retention and batched inserts."""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from audits.events import (
    AuditEvent,
    append_event,
    append_events,
    list_events,
    prune_finished_run_events,
)
from audits.models import AuditRun
from infra.tests.factories import AuditRunFactory


class AppendEventsTests(TestCase):
    def test_bulk_append_keeps_order(self):
        run = AuditRunFactory()
        append_event(run.id, "a", "scenario_attempted", {})
        append_events(run.id, "a", [("scenario_turn", {"turn": 1}), ("scenario_turn", {"turn": 2}), ("scenario_rep_started", None)])
        kinds = [(e["kind"], e["payload"].get("turn")) for e in list_events(run.id)]
        self.assertEqual(kinds, [("scenario_attempted", None), ("scenario_turn", 1), ("scenario_turn", 2), ("scenario_rep_started", None)])


class PruneTests(TestCase):
    def _run(self, status, finished_days_ago):
        run = AuditRunFactory()
        finished = timezone.now() - timedelta(days=finished_days_ago) if finished_days_ago is not None else None
        AuditRun.objects.filter(pk=run.pk).update(status=status, finished_at=finished)
        for kind in ("scenario_attempted", "scenario_turn", "scenario_turn", "scenario_rep_started", "scenario_completed"):
            append_event(run.id, "a", kind, {})
        append_event(run.id, "_run", "run_completed", {})
        return run

    def test_prunes_only_turn_events_of_old_finished_runs(self):
        old = self._run(AuditRun.Status.COMPLETED, 40)
        recent = self._run(AuditRun.Status.COMPLETED, 5)
        active = self._run(AuditRun.Status.AUDITING, None)
        self.assertEqual(prune_finished_run_events(30, batch_size=2), 3)
        self.assertEqual(
            sorted(AuditEvent.objects.filter(run_id=old.id).values_list("kind", flat=True)),
            ["run_completed", "scenario_attempted", "scenario_completed"],
        )
        self.assertEqual(AuditEvent.objects.filter(run_id=recent.id).count(), 6)
        self.assertEqual(AuditEvent.objects.filter(run_id=active.id).count(), 6)

    def test_zero_days_disables(self):
        self._run(AuditRun.Status.COMPLETED, 400)
        self.assertEqual(prune_finished_run_events(0), 0)

    def test_command(self):
        self._run(AuditRun.Status.FAILED, 40)
        out = StringIO()
        call_command("prune_audit_events", "--days", "30", stdout=out)
        self.assertIn("Deleted 3 event(s)", out.getvalue())
