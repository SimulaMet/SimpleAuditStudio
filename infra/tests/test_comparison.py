"""Tests for the comparison engine (core.comparison)."""
from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from audits.comparison import ComparisonIncompatible, compare_runs
from audits.events import ScenarioResult
from audits.models import AuditRun
from model_registry.models import ModelEndpoint
from scenarios.models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


def _build_project_with_scenarios(project, num_scenarios=2):
    """Create a scenario set with N scenarios and one published version."""
    scenarios = []
    for i in range(num_scenarios):
        s = Scenario.objects.create(project=project, key=f"scen-{i}", title=f"S{i}")
        rev = ScenarioRevision.objects.create(
            scenario=s, revision=1, description="d", expected_behavior=["x"],
            test_prompt="p", metadata={}, content_hash=f"sha256:h{i}",
        )
        scenarios.append((s, rev))
    st = ScenarioSet.objects.create(project=project, name="Test Set")
    ver = ScenarioSetVersion.objects.create(scenario_set=st, version=1, scenario_count=num_scenarios, content_hash="sha256:set")
    for pos, (s, rev) in enumerate(scenarios):
        ScenarioSetVersionItem.objects.create(version=ver, scenario=s, revision=rev, position=pos + 1)
    return scenarios, st, ver


def _make_endpoint(project, name="Model A"):
    return ModelEndpoint.objects.create(
        project=project, display_name=name, provider="openai",
        base_url="http://mock/v1", model_id="m", secret_reference="",
    )


def _make_run(project, version, target, judge, name="Run", status_val=AuditRun.Status.COMPLETED):
    return AuditRun.objects.create(
        project=project, name=name, status=status_val,
        scenario_set_version=version, target_endpoint=target, auditor_endpoint=judge,
        judge_endpoint=judge, total_scenarios=version.scenario_count, created_by=None,
        target_config_snapshot={}, auditor_config_snapshot={}, judge_config_snapshot={},
        generation_parameters_snapshot={}, simpleaudit_version="0.1.9", git_commit="abc",
    )


class ComparisonTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cmp", password="pass12345")
        self.project = Project.objects.create(name="CMP", slug="cmp")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.scenarios, self.st, self.ver = _build_project_with_scenarios(self.project, 2)
        self.ep_a = _make_endpoint(self.project, "Model A")
        self.ep_b = _make_endpoint(self.project, "Model B")
        self.judge = _make_endpoint(self.project, "Judge X")

    def _add_results(self, run, severities):
        for i, sev in enumerate(severities):
            item = run.scenario_set_version.items.order_by("position")[i]
            ScenarioResult.objects.create(
                run_id=str(run.id), version_item_id=str(item.id),
                status="completed", attempts=1, result={"severity": sev},
            )

    def test_compatible_runs_same_inputs(self):
        r1 = _make_run(self.project, self.ver, self.ep_a, self.judge, "Run 1")
        r2 = _make_run(self.project, self.ver, self.ep_b, self.judge, "Run 2")
        self._add_results(r1, ["pass", "high"])
        self._add_results(r2, ["pass", "medium"])
        result = compare_runs(self.project, [r1.id, r2.id])
        self.assertTrue(result["compatible"])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["intersection_count"], 2)
        self.assertEqual(len(result["results"]), 2)
        # Check per-scenario data
        first = result["results"][0]
        self.assertEqual(first["scenario_key"], "scen-0")
        self.assertEqual(first["runs"][str(r1.id)]["severity"], "pass")
        self.assertEqual(first["runs"][str(r2.id)]["severity"], "pass")

    def test_different_judges_warns(self):
        judge2 = _make_endpoint(self.project, "Judge Y")
        r1 = _make_run(self.project, self.ver, self.ep_a, self.judge, "Run 1")
        r2 = _make_run(self.project, self.ver, self.ep_b, judge2, "Run 2")
        self._add_results(r1, ["pass", "high"])
        self._add_results(r2, ["pass", "medium"])
        result = compare_runs(self.project, [r1.id, r2.id])
        self.assertFalse(result["compatible"])
        self.assertTrue(any("judge" in w.lower() for w in result["warnings"]))

    def test_different_versions_warns(self):
        # Create a second version of the set
        ver2 = ScenarioSetVersion.objects.create(scenario_set=self.st, version=2, scenario_count=2, content_hash="sha256:set2")
        for pos, (s, rev) in enumerate(self.scenarios):
            ScenarioSetVersionItem.objects.create(version=ver2, scenario=s, revision=rev, position=pos + 1)
        r1 = _make_run(self.project, self.ver, self.ep_a, self.judge, "Run 1")
        r2 = _make_run(self.project, ver2, self.ep_b, self.judge, "Run 2")
        self._add_results(r1, ["pass", "high"])
        self._add_results(r2, ["pass", "medium"])
        result = compare_runs(self.project, [r1.id, r2.id])
        self.assertFalse(result["compatible"])
        self.assertTrue(any("versions differ" in w.lower() for w in result["warnings"]))

    def test_missing_run_raises(self):
        r1 = _make_run(self.project, self.ver, self.ep_a, self.judge, "Run 1")
        with self.assertRaises(ComparisonIncompatible):
            compare_runs(self.project, [r1.id, 99999])

    def test_intersection_excludes_missing_scenarios(self):
        r1 = _make_run(self.project, self.ver, self.ep_a, self.judge, "Run 1")
        r2 = _make_run(self.project, self.ver, self.ep_b, self.judge, "Run 2")
        # Only add results for scen-0 in both, scen-1 only in r1
        item0 = self.ver.items.order_by("position")[0]
        item1 = self.ver.items.order_by("position")[1]
        ScenarioResult.objects.create(run_id=str(r1.id), version_item_id=str(item0.id), status="completed", attempts=1, result={"severity": "pass"})
        ScenarioResult.objects.create(run_id=str(r1.id), version_item_id=str(item1.id), status="completed", attempts=1, result={"severity": "high"})
        ScenarioResult.objects.create(run_id=str(r2.id), version_item_id=str(item0.id), status="completed", attempts=1, result={"severity": "pass"})
        # r2 has no result for scen-1
        result = compare_runs(self.project, [r1.id, r2.id])
        self.assertEqual(result["intersection_count"], 1)
        self.assertEqual(result["results"][0]["scenario_key"], "scen-0")
