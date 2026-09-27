"""New Experiment: design grid → review → launch, and the experiment page.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_experiments
"""
import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from audits.models import AuditRun, Experiment
from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    ScenarioResultFactory,
    ScenarioSetFactory,
    ScenarioSetVersionFactory,
    UserFactory,
)

_PROVENANCE = mock.Mock(version="0.2.1", commit="", source="metadata")


class _ExperimentBase(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")
        self.set_a = ScenarioSetFactory(project=self.project, name="Health")
        self.v_a = ScenarioSetVersionFactory(scenario_set=self.set_a, version=2)
        self.set_b = ScenarioSetFactory(project=self.project, name="Finance")
        self.v_b = ScenarioSetVersionFactory(scenario_set=self.set_b, version=1)
        conn = ModelConnectionFactory(project=self.project)
        self.t1 = RegisteredModelFactory(connection=conn, display_name="GPT")
        self.t2 = RegisteredModelFactory(connection=conn, display_name="Claude")
        self.judge = RegisteredModelFactory(connection=conn, display_name="Qwen")
        for target in (
            mock.patch("audits.services.resolve_engine_provenance", return_value=_PROVENANCE),
            mock.patch("audits.services.submit_audit_run"),
            mock.patch("infra.ui.submit_audit_run"),
        ):
            target.start()
            self.addCleanup(target.stop)

    def _design(self, **extra):
        data = {
            "scenario_set": [self.set_a.id],
            "target_model": [self.t1.id, self.t2.id],
            "auditor_model": [self.judge.id],
            "judge_model": [self.judge.id],
            "max_turns": "3, 5",
            "language": "",
            "n_repetitions": "3",
            "gen_config_json": '{"judge_params": {"temperature": 0}}',
        }
        data.update(extra)
        return data

    def _review_to_launch_payload(self, review, include=None, **overrides):
        data = {
            "action": "launch",
            "experiment_name": "Targets vs turns",
            "row_count": review.context["row_count"],
        }
        for key, value in review.context["design_post"]:
            data.setdefault(f"design__{key}", []).append(value)
        for row in review.context["rows"]:
            i = row["i"]
            data[f"run-{i}-spec"] = row["spec"]
            data[f"run-{i}-name"] = row["name"]
            data[f"run-{i}-max_turns"] = row["max_turns"]
            data[f"run-{i}-language"] = row["language"]
            data[f"run-{i}-n_repetitions"] = row["n_repetitions"]
            data[f"run-{i}-gen_config_json"] = row["gen_config_json"]
            if include is None or i in include:
                data[f"run-{i}-include"] = "1"
        data.update(overrides)
        return data


class ExperimentFlowTests(_ExperimentBase):
    def test_design_page_renders_multi_pickers(self):
        page = self.client.get("/experiments/new/")
        self.assertContains(page, "New Experiment")
        self.assertContains(page, 'type="checkbox" name="target_model"')
        self.assertContains(page, 'name="scenario_version"')

    def test_single_combination_launches_directly(self):
        resp = self.client.post("/experiments/new/", self._design(target_model=[self.t1.id], max_turns="3"))
        run = AuditRun.objects.get()
        self.assertRedirects(resp, f"/runs/{run.id}/", fetch_redirect_response=False)
        self.assertIsNone(run.experiment_id)
        self.assertFalse(Experiment.objects.exists())
        self.assertEqual(run.generation_parameters_snapshot["max_turns"], 3)

    def test_multi_design_shows_review_without_launching(self):
        resp = self.client.post("/experiments/new/", self._design())
        self.assertTemplateUsed(resp, "experiment_review.html")
        self.assertEqual(len(resp.context["rows"]), 4)  # 2 targets × 2 max-turn values
        self.assertEqual([k for k, _ in resp.context["columns"]], ["target"])
        self.assertEqual({r["max_turns"] for r in resp.context["rows"]}, {3, 5})
        self.assertContains(resp, "Launch")
        self.assertFalse(AuditRun.objects.exists())


    def test_launch_creates_experiment_with_selected_runs(self):
        review = self.client.post("/experiments/new/", self._design())
        payload = self._review_to_launch_payload(review, include={0, 1, 3}, **{"run-3-max_turns": "9"})
        resp = self.client.post("/experiments/new/", payload)
        exp = Experiment.objects.get()
        self.assertRedirects(resp, f"/experiments/{exp.id}/", fetch_redirect_response=False)
        self.assertEqual(exp.name, "Targets vs turns")
        self.assertEqual(exp.factors, ["target", "max_turns"])
        runs = list(exp.runs.order_by("id"))
        self.assertEqual(len(runs), 3)
        self.assertEqual({r.target_model_id for r in runs}, {self.t1.id, self.t2.id})
        # Shared settings reach every run; the per-row edit applies to its run only.
        for r in runs:
            self.assertEqual(r.generation_parameters_snapshot["n_repetitions"], 3)
            self.assertEqual(r.generation_parameters_snapshot["judge_params"], {"temperature": 0})
        self.assertEqual(runs[-1].generation_parameters_snapshot["max_turns"], 9)

    def test_multiple_scenario_sets_become_a_factor(self):
        review = self.client.post(
            "/experiments/new/", self._design(scenario_set=[self.set_a.id, self.set_b.id], target_model=[self.t1.id], max_turns="")
        )
        self.assertEqual([k for k, _ in review.context["columns"]], ["scenario_set"])
        versions = {json.loads(r["spec"])["v"] for r in review.context["rows"]}
        self.assertEqual(versions, {self.v_a.id, self.v_b.id})

    def test_back_to_design_keeps_choices(self):
        review = self.client.post("/experiments/new/", self._design())
        back = {"action": "edit"}
        for key, value in review.context["design_post"]:
            back.setdefault(f"design__{key}", []).append(value)
        page = self.client.post("/experiments/new/", back)
        self.assertTemplateUsed(page, "experiment_new.html")
        self.assertEqual(set(page.context["sel"]["target"]), {str(self.t1.id), str(self.t2.id)})
        self.assertEqual(page.context["clone"]["max_turns"], "3, 5")

    def test_design_errors_are_explained(self):
        resp = self.client.post("/experiments/new/", self._design(judge_model=[]))
        self.assertContains(resp, "Pick at least one judge model.")
        resp = self.client.post("/experiments/new/", self._design(max_turns="3, lots"))
        self.assertContains(resp, "Max turns")

    def test_run_cap(self):
        from audits.experiments import MAX_RUNS_PER_EXPERIMENT

        many = ", ".join(str(n) for n in range(1, 27))  # 2 targets × 26 = 52 runs
        resp = self.client.post("/experiments/new/", self._design(max_turns=many))
        self.assertContains(resp, f"the limit is {MAX_RUNS_PER_EXPERIMENT}")

    def test_confound_and_self_grading_warnings(self):
        review = self.client.post(
            "/experiments/new/", self._design(judge_model=[self.judge.id, self.t1.id], max_turns="")
        )
        self.assertContains(review, "Target and judge both vary")
        self.assertContains(review, "grades itself")

    def test_viewer_cannot_launch(self):
        viewer = UserFactory()
        viewer.set_password("pw")
        viewer.save()
        MembershipFactory(user=viewer, project=self.project, role="viewer")
        client = Client()
        client.login(username=viewer.username, password="pw")
        review = self.client.post("/experiments/new/", self._design())
        resp = client.post("/experiments/new/", self._review_to_launch_payload(review), follow=True)
        self.assertContains(resp, "Admin or auditor role required")
        self.assertFalse(AuditRun.objects.exists())

    def test_foreign_model_in_spec_rejected(self):
        review = self.client.post("/experiments/new/", self._design())
        foreign = RegisteredModelFactory()
        payload = self._review_to_launch_payload(review)
        spec = json.loads(payload["run-0-spec"])
        spec["t"] = foreign.id
        payload["run-0-spec"] = json.dumps(spec)
        self.client.post("/experiments/new/", payload)
        self.assertFalse(Experiment.objects.exists())  # all-or-nothing

    def test_experiment_pages(self):
        review = self.client.post("/experiments/new/", self._design())
        self.client.post("/experiments/new/", self._review_to_launch_payload(review))
        exp = Experiment.objects.get()
        run = exp.runs.order_by("id").first()
        run.status = "completed"
        run.save()
        ScenarioResultFactory(run_id=run.id, result={"severity": "pass"})
        self.assertContains(self.client.get("/experiments/"), exp.name)
        page = self.client.get(f"/experiments/{exp.id}/")
        self.assertContains(page, "Pass rate")
        self.assertContains(page, "100%")
        self.assertEqual(page.context["row_factor"], "target")
        self.assertEqual(page.context["col_factor"], "max_turns")
        swapped = self.client.get(f"/experiments/{exp.id}/?rows=max_turns&cols=")
        self.assertEqual(swapped.context["row_factor"], "max_turns")
        self.assertIsNone(swapped.context["col_factor"])
        # Run page links back to its experiment.
        self.assertContains(self.client.get(f"/runs/{run.id}/"), f"/experiments/{exp.id}/")

    def test_experiment_is_project_scoped(self):
        other = Experiment.objects.create(project=ProjectFactory(), name="Other")
        self.assertEqual(self.client.get(f"/experiments/{other.id}/").status_code, 404)


class ReviewEditingTests(_ExperimentBase):
    """Per-run settings, duplicates and error recovery on the review screen."""

    def _two_target_review(self):
        return self.client.post("/experiments/new/", self._design(max_turns="3"))

    def test_per_run_generation_config_and_language(self):
        review = self._two_target_review()
        payload = self._review_to_launch_payload(review, **{
            "run-1-gen_config_json": '{"target_params": {"temperature": 0.9}}',
            "run-1-language": "Norwegian",
        })
        self.client.post("/experiments/new/", payload)
        exp = Experiment.objects.get()
        first, second = exp.runs.order_by("id")
        self.assertEqual(first.generation_parameters_snapshot["judge_params"], {"temperature": 0})
        self.assertNotIn("target_params", first.generation_parameters_snapshot)
        self.assertEqual(second.generation_parameters_snapshot["target_params"], {"temperature": 0.9})
        self.assertEqual(second.generation_parameters_snapshot["language"], "Norwegian")
        # Factors come from the final runs, not only the design.
        self.assertEqual(exp.factors, ["target", "language", "params"])

    def test_duplicated_rows_with_gaps_are_launched(self):
        review = self._two_target_review()
        payload = self._review_to_launch_payload(review)
        # Client-side "Duplicate" of row 0 as index 5 (gaps are allowed), then
        # a removed duplicate leaves index 4 missing entirely.
        payload.update({
            "row_count": 6,
            "run-5-spec": payload["run-0-spec"],
            "run-5-name": "GPT (copy)",
            "run-5-max_turns": "8",
            "run-5-language": "",
            "run-5-n_repetitions": "3",
            "run-5-gen_config_json": "",
            "run-5-include": "1",
            "run-5-duplicate": "1",
        })
        self.client.post("/experiments/new/", payload)
        runs = list(Experiment.objects.get().runs.order_by("id"))
        self.assertEqual(len(runs), 3)
        copy = runs[-1]
        self.assertEqual(copy.name, "GPT (copy)")
        self.assertEqual(copy.generation_parameters_snapshot["max_turns"], 8)
        self.assertNotIn("judge_params", copy.generation_parameters_snapshot)

    def test_invalid_row_json_keeps_review_edits(self):
        review = self._two_target_review()
        payload = self._review_to_launch_payload(review, **{
            "run-1-gen_config_json": "{not json",
            "run-0-name": "Renamed by me",
        })
        resp = self.client.post("/experiments/new/", payload)
        self.assertTemplateUsed(resp, "experiment_review.html")
        self.assertContains(resp, "generation config is not valid JSON")
        self.assertContains(resp, "Renamed by me")
        self.assertContains(resp, "{not json")
        self.assertFalse(Experiment.objects.exists())
        # Back still restores the original design after a failed launch.
        self.assertTrue(resp.context["design_post"])

    def test_design_page_has_search_and_chips(self):
        page = self.client.get("/experiments/new/")
        self.assertContains(page, 'placeholder="Search target models…"')
        self.assertContains(page, 'placeholder="Search scenario sets…"')
        self.assertContains(page, 'data-chip-suggestions="English,Norwegian')
        self.assertContains(page, 'type="hidden" name="language"')


class ReviewWarningTests(_ExperimentBase):
    def test_shared_warnings_shown_once(self):
        # Test models have no API key: true for every run, so listed once at the top.
        review = self.client.post("/experiments/new/", self._design())
        self.assertIn("All runs: Target model has no API key set.", review.context["design_warnings"])
        self.assertTrue(all(not r["warnings"] for r in review.context["rows"]))

    def test_row_specific_warning_stays_on_its_row(self):
        review = self.client.post(
            "/experiments/new/", self._design(judge_model=[self.judge.id, self.t1.id], target_model=[self.t1.id], max_turns="")
        )
        rows = review.context["rows"]
        self.assertEqual(sum("grades itself" in " ".join(r["warnings"]) for r in rows), 1)


class ScenarioVersionPickTests(_ExperimentBase):
    def setUp(self):
        super().setUp()
        self.v_a1 = ScenarioSetVersionFactory(scenario_set=self.set_a, version=1)  # older than v_a (v2)

    def test_design_lists_versions_latest_first(self):
        page = self.client.get("/experiments/new/")
        row = next(s for s in page.context["sets"] if s.id == self.set_a.id)
        self.assertEqual([v.version for v in row.version_list], [2, 1])
        self.assertContains(page, f'name="scenario_version" value="{self.v_a1.id}"')
        self.assertContains(page, "Versions ▾")

    def test_older_version_runs_that_version(self):
        payload = self._design(target_model=[self.t1.id], max_turns="")
        del payload["scenario_set"]
        payload["scenario_version"] = [self.v_a1.id]
        self.client.post("/experiments/new/", payload)
        self.assertEqual(AuditRun.objects.get().scenario_set_version_id, self.v_a1.id)

    def test_two_versions_of_one_set_are_compared(self):
        payload = self._design(target_model=[self.t1.id], max_turns="")
        del payload["scenario_set"]
        payload["scenario_version"] = [self.v_a1.id, self.v_a.id]
        review = self.client.post("/experiments/new/", payload)
        self.assertEqual([k for k, _ in review.context["columns"]], ["scenario_set"])
        self.assertEqual({r["values"]["scenario_set"] for r in review.context["rows"]}, {"Health v1", "Health v2"})

    def test_set_id_means_latest(self):
        self.client.post("/experiments/new/", self._design(target_model=[self.t1.id], max_turns=""))
        self.assertEqual(AuditRun.objects.get().scenario_set_version_id, self.v_a.id)

    def test_clone_ticks_exact_version(self):
        run = AuditRun.objects.create(
            project=self.project, name="old", scenario_set_version=self.v_a1,
            target_model=self.t1, auditor_model=self.judge, judge_model=self.judge,
            target_config_snapshot={}, auditor_config_snapshot={}, judge_config_snapshot={},
            generation_parameters_snapshot={}, simpleaudit_version="0.2.1", git_commit="",
        )
        page = self.client.get(f"/experiments/new/?clone_from={run.id}")
        row = next(s for s in page.context["sets"] if s.id == self.set_a.id)
        self.assertEqual(row.ticked, {str(self.v_a1.id)})
        self.assertTrue(row.show_versions)  # older version ticked: list opens

    def test_version_from_other_workspace_rejected(self):
        foreign = ScenarioSetVersionFactory()
        payload = self._design()
        payload["scenario_version"] = [foreign.id]
        resp = self.client.post("/experiments/new/", payload)
        self.assertContains(resp, "not in this workspace")
        self.assertFalse(AuditRun.objects.exists())


class AlwaysLatestTests(_ExperimentBase):
    def _payload(self, versions, **extra):
        payload = self._design(target_model=[self.t1.id], max_turns="", **extra)
        del payload["scenario_set"]
        payload["scenario_version"] = versions
        return payload

    def test_design_offers_always_latest_unticked_by_default(self):
        page = self.client.get("/experiments/new/")
        self.assertContains(page, f'value="latest:{self.set_a.id}"')
        self.assertContains(page, "Always latest")
        self.assertNotContains(page, f'value="latest:{self.set_a.id}"\n                   checked')

    def test_always_latest_runs_today_and_monitor_is_unpinned(self):
        from audits.models import Monitor
        from audits.monitors import run_due_monitors

        self.client.post("/experiments/new/", self._payload(
            [f"latest:{self.set_a.id}"], repeat="168", timezone="UTC", start="now"
        ))
        run = AuditRun.objects.get()
        monitor = Monitor.objects.get()
        self.assertEqual(run.scenario_set_version_id, self.v_a.id)  # current latest (v2)
        self.assertIsNone(monitor.scenario_set_version_id)  # follows new versions
        self.assertIn("always latest", monitor.name)
        self.assertEqual(run.monitor_id, monitor.id)  # still the first point
        # A new version is published; the next tick uses it.
        v3 = ScenarioSetVersionFactory(scenario_set=self.set_a, version=3)
        AuditRun.objects.update(status="completed")
        Monitor.objects.update(next_run_at=timezone.now())
        new_id = run_due_monitors(now=timezone.now() + timedelta(seconds=1))[0]
        self.assertEqual(AuditRun.objects.get(pk=new_id).scenario_set_version_id, v3.id)

    def test_pinned_and_always_latest_are_compared(self):
        review = self.client.post("/experiments/new/", self._payload([f"latest:{self.set_a.id}", self.v_a.id]))
        labels = {r["values"]["scenario_set"] for r in review.context["rows"]}
        self.assertEqual(labels, {"Health v2", "Health (always latest, now v2)"})

    def test_always_latest_for_foreign_set_rejected(self):
        foreign = ScenarioSetFactory()
        ScenarioSetVersionFactory(scenario_set=foreign)
        resp = self.client.post("/experiments/new/", self._payload([f"latest:{foreign.id}"]))
        self.assertContains(resp, "not in this workspace")
