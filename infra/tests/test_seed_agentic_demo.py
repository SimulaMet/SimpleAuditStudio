"""The recorded agentic demo run loads into a new workspace without chat or API keys.

``infra/fixtures/agentic_demo_run.json`` is a real agentic audit of the demo
agent against the default agentic pack (D01-D10), relabelled to a neutral
"Demo model (recorded)". These tests load the committed fixture as the seed does.
"""
import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from audits.models import AuditRun
from infra.management.commands.seed_agentic_demo import FIXTURE_PATH
from infra.seed import seed_default_model_connections, seed_demo_agent, seed_workspace
from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory

RECORDING_IDENTIFIERS = ("Qwen", "127.0.0.1", "sk-")


class AgenticDemoFixtureTests(TestCase):
    def test_the_committed_fixture_is_a_relabelled_real_recording(self):
        from audits.agentic.demo_fixture import validate_fixture

        raw = FIXTURE_PATH.read_text()
        fixture = json.loads(raw)
        validate_fixture(fixture, require_recorded=True)   # format, provenance, checksum
        self.assertEqual(fixture["recorded_run"]["models_relabelled"], "Demo model (recorded)")
        for identifier in RECORDING_IDENTIFIERS:
            self.assertNotIn(identifier, raw)
        run = fixture["run"]
        for snapshot in (run["auditor_config_snapshot"], run["judge_config_snapshot"],
                         run["target_config_snapshot"], run["agent_config_snapshot"]["base_model"]):
            self.assertNotIn("base_url", snapshot)   # where it ran is not part of the demo
        keys = sorted(spec["scenario_key"] for spec in fixture["scenario_results"])
        self.assertEqual([key[:11] for key in keys], [f"agentic_d{n:02d}" for n in range(1, 11)])
        self.assertTrue(fixture["spans"])


class SeedAgenticDemoTests(TestCase):
    def setUp(self):
        self.user = UserFactory(is_superuser=True)
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        seed_workspace(self.project, self.user)
        seed_default_model_connections(self.project, self.user)
        seed_demo_agent(self.project, self.user, log=lambda *_: None)   # chat off: local rows only
        call_command("seed_agentic_scenarios", project=self.project.id, stdout=StringIO())

    def _seed(self, *args):
        out = StringIO()
        call_command("seed_agentic_demo", *args, project=self.project.id, stdout=out)
        return out.getvalue()

    def _run(self):
        return AuditRun.objects.filter(project=self.project, runtime_metadata__agentic_demo_seed=True).first()

    def test_the_recorded_run_loads_with_results_spans_and_the_demo_agent(self):
        from audits.events import ScenarioResult
        from model_registry.models import OtlpSpan

        self._seed()
        run = self._run()
        self.assertIsNotNone(run)
        self.assertEqual(run.status, AuditRun.Status.COMPLETED)
        self.assertEqual(run.agent.name, "Support Refund Assistant")
        self.assertEqual(run.scenario_set_version.scenario_set.name, "Agentic Audit: Explanatory Scenarios")
        self.assertEqual(run.total_scenarios, 10)
        self.assertEqual(ScenarioResult.objects.filter(run_id=run.pk).count(), 10)
        self.assertEqual(run.runtime_metadata["source"], "recorded_real_run_fixture")
        # Neutral labels in the frozen record; rows a new user can re-run with.
        self.assertEqual(run.auditor_config_snapshot["display_name"], "Demo model (recorded)")
        self.assertEqual(run.auditor_model.display_name, "GPT-4o Mini")
        self.assertEqual(run.judge_config_snapshot["judge"]["version_id"], run.judge_version_id)
        target_id = run.trace_config["target_id"]
        self.assertTrue(OtlpSpan.objects.filter(target_id=target_id).exists())
        self.assertIn("agentic_evaluation", run.summary_metrics)

    def test_it_is_idempotent_and_a_deleted_run_stays_deleted(self):
        self._seed()
        self._seed()
        self.assertEqual(AuditRun.objects.filter(runtime_metadata__agentic_demo_seed=True).count(), 1)
        self._run().delete()
        self.assertIn("already given", self._seed())
        self.assertIsNone(self._run())
        self._seed("--force")
        self.assertIsNotNone(self._run())

    def test_it_is_skipped_without_the_demo_agent(self):
        from model_registry.models import Agent

        Agent.objects.filter(project=self.project).delete()
        self.assertIn("skipped", self._seed())
        self.assertIsNone(self._run())

    def test_a_loaded_demo_run_cannot_be_exported_as_a_new_recording(self):
        from audits.agentic.demo_fixture import (
            FixtureValidationError,
            export_run_fixture,
        )

        self._seed()
        run = self._run()
        run.runtime_metadata = {**run.runtime_metadata, "executed": True}
        with self.assertRaises(FixtureValidationError):
            export_run_fixture(run)

    def test_seed_platform_loads_it(self):
        call_command("seed_platform", project=self.project.id, skip_packs=True, skip_demo_agent=True,
                     stdout=StringIO(), stderr=StringIO())
        self.assertIsNotNone(self._run())

    def test_the_run_and_its_results_render(self):
        from audits.events import ScenarioResult

        self._seed()
        run = self._run()
        self.client.force_login(self.user)
        page = self.client.get(f"/runs/{run.pk}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Support Refund Assistant")
        result = ScenarioResult.objects.filter(run_id=run.pk).order_by("id").first()
        detail = self.client.get(f"/runs/{run.pk}/results/{result.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "trace-span-")   # trace evidence is linked on the result page

    def test_it_is_named_like_the_other_demo_runs(self):
        self._seed()
        self.assertEqual(self._run().name, "Demo: Agentic audit: Support Refund Assistant (D01-D10)")
