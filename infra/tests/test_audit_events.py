"""Tests for the durable audit event / idempotent result layer.

These lock in the two invariants the Phase 4 spike validated against a SQLite
stand-in, now that the real Postgres-backed models exist:

- ``AuditEvent`` is append-only with a monotonic id so SSE can replay from
  ``Last-Event-ID`` without losing progress.
- ``ScenarioResult`` writes are idempotent keyed on ``(run_id, version_item_id)``
  with ``attempts = MAX(...)`` so duplicate execution never creates duplicates.
"""
from django.test import TestCase

from audits.events import (
    append_event,
    count_results,
    get_result,
    list_events,
    upsert_scenario_result,
)


class AuditEventReplayTest(TestCase):
    def test_events_are_monotonic_and_replayable_from_last_event_id(self):
        first = append_event(1, "a", "scenario_attempted", {"attempt": 1})
        second = append_event(1, "a", "scenario_completed", {"attempt": 1})
        third = append_event(1, "b", "scenario_attempted", {"attempt": 1})

        self.assertTrue(first < second < third)

        all_events = list_events(1)
        self.assertEqual(len(all_events), 3)

        # A client reconnecting with Last-Event-ID == second should fetch only what it missed.
        after_second = list_events(1, after_id=second)
        self.assertEqual([e["id"] for e in after_second], [third])
        self.assertEqual(after_second[0]["kind"], "scenario_attempted")
        self.assertEqual(after_second[0]["version_item_id"], "b")

    def test_events_are_scoped_per_run(self):
        append_event(1, "a", "scenario_attempted")
        append_event(2, "a", "scenario_attempted")
        self.assertEqual(len(list_events(1)), 1)
        self.assertEqual(len(list_events(2)), 1)


class ScenarioResultIdempotencyTest(TestCase):
    def test_duplicate_execution_does_not_create_duplicate_rows(self):
        upsert_scenario_result(1, "a", status="completed", attempts=1)
        upsert_scenario_result(1, "a", status="completed", attempts=1)
        upsert_scenario_result(1, "a", status="completed", attempts=1)
        self.assertEqual(count_results(1), 1)

    def test_attempts_keep_max_across_retries(self):
        upsert_scenario_result(1, "a", status="failed", attempts=1)
        upsert_scenario_result(1, "a", status="failed", attempts=2)
        upsert_scenario_result(1, "a", status="completed", attempts=3)
        result = get_result(1, "a")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], 3)

        # A lower attempt number must not regress the stored max.
        upsert_scenario_result(1, "a", status="completed", attempts=2)
        result = get_result(1, "a")
        self.assertEqual(result["attempts"], 3)

    def test_distinct_version_items_are_tracked_separately(self):
        upsert_scenario_result(1, "a", status="completed", attempts=1)
        upsert_scenario_result(1, "b", status="completed", attempts=1)
        self.assertEqual(count_results(1), 2)
        self.assertIsNotNone(get_result(1, "a"))
        self.assertIsNotNone(get_result(1, "b"))


class CompletedResultNotDowngradedTest(TestCase):
    def test_late_failed_duplicate_does_not_replace_completed(self):
        upsert_scenario_result(1, "a", status="completed", attempts=1, result={"ok": True})
        upsert_scenario_result(1, "a", status="failed", attempts=3, result={"ok": False})
        result = get_result(1, "a")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], 3)
