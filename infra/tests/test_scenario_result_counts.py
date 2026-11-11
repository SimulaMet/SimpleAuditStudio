"""ScenarioResult trial tallies: derived columns, upsert path and pass_counts."""
from django.test import TestCase

from audits.events import ScenarioResult, trial_counts, upsert_scenario_result
from audits.monitors import pass_counts
from infra.tests.factories import AuditRunFactory, ScenarioResultFactory


class TrialCountsTests(TestCase):
    def test_single_trial_severities(self):
        self.assertEqual(
            trial_counts("completed", {"severity": "pass"}),
            {"n_pass": 1, "n_judged": 1, "n_errors": 0, "n_ungraded": 0},
        )
        self.assertEqual(trial_counts("completed", {"severity": "high"})["n_pass"], 0)
        self.assertEqual(trial_counts("completed", {"severity": "ERROR"})["n_errors"], 1)
        self.assertEqual(trial_counts("completed", {"severity": "ungraded"})["n_ungraded"], 1)
        self.assertEqual(trial_counts("completed", {})["n_errors"], 1)

    def test_repetitions_count_as_separate_trials(self):
        result = {"reps": [{"severity": "pass"}, {"severity": "high"}, {"severity": "ERROR"}, None]}
        self.assertEqual(
            trial_counts("completed", result),
            {"n_pass": 1, "n_judged": 2, "n_errors": 2, "n_ungraded": 0},
        )

    def test_non_completed_row_is_one_error(self):
        self.assertEqual(
            trial_counts("failed", {"severity": "pass"}),
            {"n_pass": 0, "n_judged": 0, "n_errors": 1, "n_ungraded": 0},
        )


class SaveKeepsCountsInSyncTests(TestCase):
    def test_upsert_creates_then_updates_counts(self):
        run = AuditRunFactory()
        upsert_scenario_result(run.id, "1", status="failed", attempts=1, result={})
        row = ScenarioResult.objects.get(run_id=run.id, version_item_id="1")
        self.assertEqual((row.n_errors, row.n_judged), (1, 0))
        upsert_scenario_result(run.id, "1", status="completed", attempts=2, result={"severity": "pass"})
        row.refresh_from_db()
        self.assertEqual((row.n_pass, row.n_judged, row.n_errors), (1, 1, 0))

    def test_late_failure_does_not_overwrite_stored_success(self):
        run = AuditRunFactory()
        upsert_scenario_result(run.id, "1", status="completed", attempts=1, result={"severity": "pass"})
        upsert_scenario_result(run.id, "1", status="failed", attempts=2, result={})
        row = ScenarioResult.objects.get(run_id=run.id, version_item_id="1")
        self.assertEqual((row.status, row.n_pass, row.n_errors), ("completed", 1, 0))


class PassCountsTests(TestCase):
    def test_sums_per_run_and_defaults_missing_runs(self):
        a, b, empty = AuditRunFactory(), AuditRunFactory(), AuditRunFactory()
        ScenarioResultFactory(run_id=a.id, version_item_id="1", result={"severity": "pass"})
        ScenarioResultFactory(run_id=a.id, version_item_id="2", result={"reps": [{"severity": "high"}, {"severity": "pass"}]})
        ScenarioResultFactory(run_id=a.id, version_item_id="3", status="failed", result={})
        ScenarioResultFactory(run_id=b.id, version_item_id="1", result={"severity": "ungraded"})
        counts = pass_counts([a.id, b.id, empty.id])
        self.assertEqual(counts[a.id], {"k": 2, "n": 3, "errors": 1, "ungraded": 0})
        self.assertEqual(counts[b.id], {"k": 0, "n": 0, "errors": 0, "ungraded": 1})
        self.assertEqual(counts[empty.id], {"k": 0, "n": 0, "errors": 0, "ungraded": 0})

    def test_never_loads_result_json(self):
        run = AuditRunFactory()
        ScenarioResultFactory(run_id=run.id, version_item_id="1")
        with self.assertNumQueries(1) as ctx:
            pass_counts([run.id])
        self.assertNotIn('"result"', ctx.captured_queries[0]["sql"].split("FROM")[0])


class BackfillMigrationTests(TestCase):
    def test_backfill_matches_runtime_counts(self):
        from importlib import import_module

        from django.apps import apps

        mod = import_module("audits.migrations.0011_scenario_result_trial_counts")
        run = AuditRunFactory()
        ScenarioResultFactory(run_id=run.id, version_item_id="1", result={"severity": "pass"})
        ScenarioResultFactory(run_id=run.id, version_item_id="2", result={"reps": [{"severity": "high"}, {"severity": "ungraded"}]})
        ScenarioResultFactory(run_id=run.id, version_item_id="3", status="failed", result={})
        expected = pass_counts([run.id])
        ScenarioResult.objects.update(n_pass=0, n_judged=0, n_errors=0, n_ungraded=0)
        mod.backfill_trial_counts(apps, None)
        self.assertEqual(pass_counts([run.id]), expected)


class ListSummaryTests(TestCase):
    def test_projection_drops_transcripts_and_follows_result_updates(self):
        from audits.events import result_projection

        run = AuditRunFactory()
        big = {
            "severity": "pass", "summary": "ok", "conversation": [{"role": "user", "content": "x" * 5000}],
            "agentic_evaluation": {"status": "PASS"},
        }
        row = ScenarioResultFactory(run_id=run.id, version_item_id="1", result=big)
        self.assertEqual(row.list_summary["summary"], "ok")
        self.assertEqual(row.list_summary["agentic_status"], "PASS")
        self.assertNotIn("conversation", row.list_summary)
        upsert_scenario_result(run.id, "1", status="completed", attempts=1, result={"severity": "high", "summary": "bad"})
        row.refresh_from_db()
        self.assertEqual(row.list_summary, result_projection({"severity": "high", "summary": "bad"}))

    def test_repeated_result_keeps_rep_summaries(self):
        run = AuditRunFactory()
        row = ScenarioResultFactory(
            run_id=run.id, version_item_id="1",
            result={"aggregated_severity": "high", "agreement_rate": 0.5,
                    "reps": [{"severity": "pass", "summary": "a", "conversation": ["x"]},
                             {"severity": "high", "summary": "b"}]},
        )
        self.assertTrue(row.list_summary["has_reps"])
        self.assertEqual(row.list_summary["reps"], [{"severity": "pass", "summary": "a"}, {"severity": "high", "summary": "b"}])
