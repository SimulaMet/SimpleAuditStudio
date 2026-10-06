import json
import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from audits.events import AuditEvent, ScenarioResult
from audits.models import AuditRun
from infra.seed import seed_default_model_connections, seed_workspace
from infra.tests.factories import ProjectFactory, UserFactory
from model_registry.models import (
    Agent,
    ModelConnection,
    OtlpSpan,
    RegisteredModel,
    Tool,
)
from model_registry.services import agent_target_model


class EmbeddedOtelDefaultsTests(TestCase):
    def test_structural_otel_is_on_by_default_but_explicit_opt_out_is_preserved(self):
        from simpleaudit_studio.cli import _set_embedded_chat_defaults

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIMPLEAUDIT_CHAT_OTLP", None)
            os.environ.pop("SIMPLEAUDIT_CHAT", None)
            _set_embedded_chat_defaults(disable_chat=False)
            self.assertEqual(os.environ["SIMPLEAUDIT_CHAT_OTLP"], "true")
            self.assertEqual(os.environ["SIMPLEAUDIT_CHAT"], "embedded")

        with patch.dict(os.environ, {"SIMPLEAUDIT_CHAT_OTLP": "false"}, clear=False):
            _set_embedded_chat_defaults(disable_chat=False)
            self.assertEqual(os.environ["SIMPLEAUDIT_CHAT_OTLP"], "false")

    def test_disabling_chat_does_not_enable_structural_otel(self):
        from simpleaudit_studio.cli import _set_embedded_chat_defaults

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIMPLEAUDIT_CHAT_OTLP", None)
            _set_embedded_chat_defaults(disable_chat=True)
            self.assertNotIn("SIMPLEAUDIT_CHAT_OTLP", os.environ)
            self.assertEqual(os.environ["SIMPLEAUDIT_CHAT"], "off")


class EmbeddedSeedIntegrationTests(TestCase):
    def test_first_run_seeds_agentic_scenarios_without_audit_execution(self):
        from accounts.models import Project
        from audits.models import AuditRun
        from scenarios.models import ScenarioSet
        from simpleaudit_studio.cli import _seed_demo_data

        with patch.dict(os.environ, {
            "BOOTSTRAP_USERNAME": "studio",
            "BOOTSTRAP_EMAIL": "admin@localhost",
            "BOOTSTRAP_PASSWORD": "test-password",
            "BOOTSTRAP_PROJECT_NAME": "Demo Project",
        }):
            _seed_demo_data()

        project = Project.objects.get(name="Demo Project")
        self.assertTrue(ScenarioSet.objects.filter(
            project=project, name="Acme Agentic Safety"
        ).exists())
        self.assertFalse(AuditRun.objects.filter(
            project=project, runtime_metadata__agentic_demo_seed=True
        ).exists())

    def test_completed_fixture_is_seeded_only_after_chat_and_resource_sync(self):
        from simpleaudit_studio import cli

        calls = []

        class InlineThread:
            def __init__(self, *, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        class Process:
            def poll(self):
                return None

        class ChatProxy:
            def start_open_webui(self, port):
                calls.append("start")
                return Process()

            def serve(self, port):
                calls.append("serve")

            def wait_until_ready(self, process):
                calls.append("ready")
                return True

        with (
            patch.object(cli.threading, "Thread", InlineThread),
            patch.object(cli, "_sync_chat_models", side_effect=lambda: calls.append("models") or "synced"),
            patch.object(cli, "_backfill_demo_chat", side_effect=lambda: calls.append("resources") or ""),
            patch.object(cli, "_backfill_agentic_demo", side_effect=lambda: calls.append("fixture") or ""),
        ):
            cli.start_chat(ChatProxy(), 8001)

        self.assertEqual(calls, ["start", "serve", "ready", "models", "resources", "fixture"])


class SeedAgenticDemoTests(TestCase):
    def setUp(self):
        self.user = UserFactory(is_superuser=True)
        self.project = ProjectFactory()
        seed_workspace(self.project, self.user)
        seed_default_model_connections(self.project, self.user)
        call_command("seed_agentic_scenarios", project=self.project.id, stdout=StringIO())
        base_model = RegisteredModel.objects.filter(project=self.project).exclude(
            connection__name="Open WebUI Agents"
        ).first()
        agent = Agent.objects.create(
            project=self.project,
            name="Support Refund Assistant",
            base_model=base_model,
            external_id="studio.agent-demo",
            capabilities={"knowledge_search": True},
            created_by=self.user,
        )
        tool = Tool.objects.create(
            project=self.project,
            name="Acme Order Lookup",
            external_id="acme_order_lookup",
            created_by=self.user,
        )
        agent.tools.set([tool])
        connection, _ = ModelConnection.objects.get_or_create(
            project=self.project,
            name="Open WebUI Agents",
            defaults={"base_url": "http://localhost:8080/api/v1", "created_by": self.user},
        )
        RegisteredModel.objects.create(
            project=self.project,
            connection=connection,
            display_name=agent.name,
            model_id=agent.external_id,
            created_by=self.user,
        )

    def test_seeded_scenario_set_uses_a_customer_facing_name(self):
        from scenarios.models import ScenarioSet

        self.assertTrue(
            ScenarioSet.objects.filter(
                project=self.project, name="Acme Agentic Safety"
            ).exists()
        )
        self.assertFalse(
            ScenarioSet.objects.filter(
                project=self.project, name="Acme Agentic Safety (OTEL)"
            ).exists()
        )

    def test_reseeding_scenarios_does_not_publish_an_unchanged_version(self):
        from scenarios.models import ScenarioSet, ScenarioSetVersion

        scenario_set = ScenarioSet.objects.get(
            project=self.project, name="Acme Agentic Safety"
        )
        version_count = ScenarioSetVersion.objects.filter(scenario_set=scenario_set).count()

        call_command("seed_agentic_scenarios", project=self.project.id, stdout=StringIO())

        self.assertEqual(
            ScenarioSetVersion.objects.filter(scenario_set=scenario_set).count(),
            version_count,
        )

    def test_reseeding_backfills_agentic_rollup_for_existing_demo_run(self):
        run = self._seed_preloaded_run()
        run.summary_metrics.pop("agentic_evaluation", None)
        run.save(update_fields=["summary_metrics"])

        call_command("seed_agentic_demo", project=self.project.id, stdout=StringIO())

        run.refresh_from_db()
        self.assertEqual(
            run.summary_metrics["agentic_evaluation"],
            {"total": 8, "passed": 6, "failed": 1, "inconclusive": 1},
        )
        self.assertEqual(
            (run.summary_metrics["passed"], run.summary_metrics["failed"], run.summary_metrics["inconclusive"]),
            (6, 1, 1),
        )

    def test_run_detail_displays_agentic_outcome_rollup(self):
        run = self._seed_preloaded_run()
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

        response = self.client.get(f"/runs/{run.pk}/")

        self.assertContains(response, "Agentic checks:")
        self.assertContains(response, "6 pass · 1 fail · 1 inconclusive")

    def _seed_preloaded_run(self):
        call_command("seed_agentic_demo", project=self.project.id, stdout=StringIO())
        return AuditRun.objects.get(
            project=self.project, runtime_metadata__agentic_demo_seed=True
        )

    def test_reseeding_updates_existing_scenario_titles(self):
        from infra.management.commands.seed_agentic_scenarios import SCENARIOS
        from scenarios.models import Scenario

        scenario = Scenario.objects.get(project=self.project, key="a01")
        scenario.title = "Acme Agentic Safety A01"
        scenario.save(update_fields=["title"])

        call_command("seed_agentic_scenarios", project=self.project.id, stdout=StringIO())

        scenario.refresh_from_db()
        self.assertEqual(scenario.title, SCENARIOS["A01"]["title"])

    def test_preloading_preserves_disabled_otlp_credential(self):
        from audits.models import AuditRun
        from model_registry.otlp_services import create_credential

        target = agent_target_model(Agent.objects.get(project=self.project))
        credential = create_credential(
            project=self.project, connection=target.connection, auth_mode="basic", user=self.user
        ).credential
        credential.enabled = False
        credential.save(update_fields=["enabled"])

        call_command("seed_agentic_demo", project=self.project.id, stdout=StringIO())

        credential.refresh_from_db()
        self.assertFalse(credential.enabled)
        self.assertTrue(AuditRun.objects.filter(
            project=self.project, runtime_metadata__agentic_demo_seed=True
        ).exists())

    def test_preloading_does_not_replace_an_existing_otlp_credential(self):
        from model_registry.otlp_services import create_credential

        target = agent_target_model(Agent.objects.get(project=self.project))
        credential = create_credential(
            project=self.project, connection=target.connection, auth_mode="basic", user=self.user
        ).credential

        call_command("seed_agentic_demo", project=self.project.id, stdout=StringIO())

        credential.refresh_from_db()
        self.assertEqual(credential.auth_mode, "basic")

    def test_seed_preloads_completed_results_and_trace_evidence_idempotently(self):
        with patch("infra.engine.run_scenario", side_effect=AssertionError("must not execute")) as run_scenario:
            call_command("seed_agentic_demo", project=self.project.id, stdout=StringIO())
            run_scenario.assert_not_called()

        run = AuditRun.objects.get(project=self.project, runtime_metadata__agentic_demo_seed=True)
        self.assertEqual(run.status, AuditRun.Status.COMPLETED)
        self.assertEqual(
            run.summary_metrics["agentic_evaluation"],
            {"total": 8, "passed": 6, "failed": 1, "inconclusive": 1},
        )
        self.assertEqual(
            (run.summary_metrics["passed"], run.summary_metrics["failed"], run.summary_metrics["inconclusive"]),
            (6, 1, 1),
        )
        self.assertEqual(
            run.summary_metrics["text_judgment"],
            {"total": 8, "severity_distribution": {"pass": 8}},
        )
        self.assertEqual(run.agent_id, Agent.objects.get(project=self.project).id)
        self.assertEqual(run.target_model_id, agent_target_model(run.agent).id)
        self.assertEqual(ScenarioResult.objects.filter(run_id=run.id).count(), 8)
        results = list(ScenarioResult.objects.filter(run_id=run.id))
        self.assertTrue(all(row.result.get("agentic_evaluation") for row in results))
        self.assertTrue(all(row.result.get("trace_ids") for row in results))
        item_keys = {
            str(item.pk): item.scenario.key.upper()
            for item in run.scenario_set_version.items.select_related("scenario")
        }
        by_key = {item_keys[row.version_item_id]: row.result for row in results}
        self.assertEqual(by_key["A04"]["judgment"]["severity"], "pass")
        self.assertEqual(by_key["A04"]["agentic_evaluation"]["status"], "FAIL")
        for key in ("A01", "A02", "A03", "A06", "A07", "A13"):
            self.assertEqual(
                by_key[key]["agentic_evaluation"]["status"],
                "PASS",
                f"{key}: {by_key[key]['agentic_evaluation']['checks']}",
            )
        self.assertEqual(by_key["A12"]["agentic_evaluation"]["status"], "INCONCLUSIVE")
        self.assertTrue(by_key["A04"]["demo_fixture"]["source"] == "synthetic")
        from infra.ui import _rep_view

        self.assertEqual(_rep_view(by_key["A04"], 0)["trace"]["total_spans"], 2)
        spans = OtlpSpan.objects.filter(target_id=run.trace_config["target_id"])
        self.assertEqual(spans.count(), 16)
        a12_id = by_key["A12"]["trace_ids"][0]
        a12_spans = spans.filter(trace_id=a12_id)
        self.assertFalse(any("gen_ai.tool.call.arguments" in span.attributes for span in a12_spans))
        a13_id = by_key["A13"]["trace_ids"][0]
        a13_spans = spans.filter(trace_id=a13_id)
        self.assertTrue(any("gen_ai.tool.call.arguments" in span.attributes for span in a13_spans))
        self.assertTrue(AuditEvent.objects.filter(run_id=run.id, kind="run_completed").exists())

        call_command("seed_agentic_demo", project=self.project.id, stdout=StringIO())
        self.assertEqual(AuditRun.objects.filter(project=self.project, runtime_metadata__agentic_demo_seed=True).count(), 1)
        self.assertEqual(ScenarioResult.objects.filter(run_id=run.id).count(), 8)
        self.assertEqual(OtlpSpan.objects.filter(target_id=run.trace_config["target_id"]).count(), 16)

    def test_capture_exports_db_results_and_spans_only_with_explicit_execution_provenance(self):
        from audits.agentic.demo_fixture import export_run_fixture, validate_fixture

        run = self._seed_preloaded_run()
        metadata = dict(run.runtime_metadata)
        metadata.pop("agentic_demo_seed", None)
        metadata.update({"source": "live_run", "executed": True})
        run.runtime_metadata = metadata
        run.save(update_fields=["runtime_metadata"])

        fixture = export_run_fixture(run)
        validate_fixture(fixture, require_recorded=True)
        self.assertEqual(len(fixture["scenario_results"]), 8)
        self.assertEqual(len(fixture["spans"]), 16)
        self.assertEqual(fixture["recorded_run"]["source"], "recorded_real_run_fixture")

        with tempfile.TemporaryDirectory() as directory:
            output = f"{directory}/captured.json"
            call_command(
                "capture_agentic_demo_fixture", "--run", str(run.pk),
                "--output", output, "--require-recorded", stdout=StringIO(),
            )
            with open(output) as captured:
                written = json.load(captured)
        self.assertEqual(written["_meta"]["sanitized"], True)
        self.assertEqual(written["recorded_run"]["executed"], True)
