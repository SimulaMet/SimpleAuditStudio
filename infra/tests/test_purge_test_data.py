"""Tests for the ``purge_test_data`` management command.

Verifies that the command deletes E2E/smoke artifacts (runs, events, results,
endpoints, scenarios, sets) matching the configured prefixes, leaves non-matching
rows alone, and honors --dry-run (no deletion).
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from audits.events import AuditEvent, ScenarioResult
from audits.models import AuditRun
from model_registry.models import ModelEndpoint
from scenarios.models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


def _make_e2e_artifacts(project):
    """Create a minimal set of E2E-named artifacts plus one non-E2E row each."""
    # E2E scenario + revision
    e2e_scenario = Scenario.objects.create(project=project, key="e2e-safe-refusal-1", title="E2E")
    e2e_rev = ScenarioRevision.objects.create(
        scenario=e2e_scenario, revision=1, description="d", expected_behavior=["x"],
        test_prompt="p", metadata={}, content_hash="sha256:e2e",
    )
    # Non-E2E scenario (must survive)
    keep_scenario = Scenario.objects.create(project=project, key="real-scenario", title="Keep")
    keep_rev = ScenarioRevision.objects.create(
        scenario=keep_scenario, revision=1, description="d", expected_behavior=["x"],
        test_prompt="p", metadata={}, content_hash="sha256:keep",
    )

    # E2E set + version + item
    e2e_set = ScenarioSet.objects.create(project=project, name="E2E Set 1")
    e2e_version = ScenarioSetVersion.objects.create(
        scenario_set=e2e_set, version=1, scenario_count=1, content_hash="sha256:set"
    )
    ScenarioSetVersionItem.objects.create(version=e2e_version, scenario=e2e_scenario, revision=e2e_rev, position=1)

    # Non-E2E set + version (must survive)
    keep_set = ScenarioSet.objects.create(project=project, name="Real Set")
    keep_version = ScenarioSetVersion.objects.create(
        scenario_set=keep_set, version=1, scenario_count=1, content_hash="sha256:keepset"
    )
    ScenarioSetVersionItem.objects.create(version=keep_version, scenario=keep_scenario, revision=keep_rev, position=1)

    # E2E endpoint + non-E2E endpoint
    e2e_ep = ModelEndpoint.objects.create(
        project=project, display_name="Mock Model (E2E 1)", provider="openai",
        base_url="http://mock:8901/v1", model_id="mock-model", secret_reference="",
    )
    keep_ep = ModelEndpoint.objects.create(
        project=project, display_name="Production Model", provider="openai",
        base_url="https://real.example/v1", model_id="real", secret_reference="REAL_KEY",
    )

    # E2E run + non-E2E run
    e2e_run = AuditRun.objects.create(
        project=project, name="E2E Smoke Run 1", status=AuditRun.Status.COMPLETED,
        scenario_set_version=e2e_version, target_endpoint=e2e_ep, auditor_endpoint=e2e_ep,
        judge_endpoint=e2e_ep, total_scenarios=1, created_by=None,
        target_config_snapshot={}, auditor_config_snapshot={}, judge_config_snapshot={},
        generation_parameters_snapshot={}, simpleaudit_version="0.1.9", git_commit="abc",
    )
    keep_run = AuditRun.objects.create(
        project=project, name="Important Real Run", status=AuditRun.Status.COMPLETED,
        scenario_set_version=keep_version, target_endpoint=keep_ep, auditor_endpoint=keep_ep,
        judge_endpoint=keep_ep, total_scenarios=1, created_by=None,
        target_config_snapshot={}, auditor_config_snapshot={}, judge_config_snapshot={},
        generation_parameters_snapshot={}, simpleaudit_version="0.1.9", git_commit="abc",
    )

    # Durable events + results for the E2E run
    AuditEvent.objects.create(run_id=str(e2e_run.id), version_item_id="_run", kind="run_completed", payload={})
    ScenarioResult.objects.create(run_id=str(e2e_run.id), version_item_id="1", status="completed", attempts=1, result={"severity": "pass"})

    return {
        "e2e_scenario": e2e_scenario, "keep_scenario": keep_scenario,
        "e2e_set": e2e_set, "keep_set": keep_set,
        "e2e_ep": e2e_ep, "keep_ep": keep_ep,
        "e2e_run": e2e_run, "keep_run": keep_run,
    }


class PurgeTestDataTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="purger", password="pass12345")
        self.project = Project.objects.create(name="Purge", slug="purge")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.art = _make_e2e_artifacts(self.project)

    def test_dry_run_deletes_nothing(self):
        out = StringIO()
        call_command("purge_test_data", "--dry-run", stdout=out)
        output = out.getvalue()
        self.assertIn("Dry run", output)
        # Nothing deleted.
        self.assertEqual(AuditRun.objects.count(), 2)
        self.assertEqual(Scenario.objects.count(), 2)
        self.assertEqual(ModelEndpoint.objects.count(), 2)
        self.assertEqual(ScenarioSet.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertEqual(ScenarioResult.objects.count(), 1)

    def test_purge_removes_e2e_artifacts_only(self):
        out = StringIO()
        call_command("purge_test_data", stdout=out)
        # E2E rows gone.
        self.assertFalse(AuditRun.objects.filter(id=self.art["e2e_run"].id).exists())
        self.assertFalse(Scenario.objects.filter(id=self.art["e2e_scenario"].id).exists())
        self.assertFalse(ModelEndpoint.objects.filter(id=self.art["e2e_ep"].id).exists())
        self.assertFalse(ScenarioSet.objects.filter(id=self.art["e2e_set"].id).exists())
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertEqual(ScenarioResult.objects.count(), 0)
        # Non-E2E rows survive.
        self.assertTrue(AuditRun.objects.filter(id=self.art["keep_run"].id).exists())
        self.assertTrue(Scenario.objects.filter(id=self.art["keep_scenario"].id).exists())
        self.assertTrue(ModelEndpoint.objects.filter(id=self.art["keep_ep"].id).exists())
        self.assertTrue(ScenarioSet.objects.filter(id=self.art["keep_set"].id).exists())

    def test_purge_is_idempotent(self):
        out = StringIO()
        call_command("purge_test_data", stdout=out)
        count_after_first = AuditRun.objects.count()
        out2 = StringIO()
        call_command("purge_test_data", stdout=out2)
        self.assertEqual(AuditRun.objects.count(), count_after_first)
