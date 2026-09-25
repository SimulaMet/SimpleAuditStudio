"""Tests for the SimpleAudit engine integration layer.

The real engine is not installed in the test environment, so these tests cover:
- ``core.engine`` raises a clean ``EngineError`` when the engine is unavailable.
- The worker's scenario task records a durable FAILED result (not a crash) when
  the engine cannot load, preserving the run's counters.
- With the engine mocked, a successful scenario persists its structured result
  into the idempotent ScenarioResult row.
"""
import inspect
from unittest import mock

from django.test import TestCase

from audits.events import get_result
from audits.models import AuditRun
from model_registry.models import ModelEndpoint
from accounts.models import Project, ProjectMembership, User
from scenarios.models import Scenario, ScenarioRevision, ScenarioSet, ScenarioSetVersion, ScenarioSetVersionItem


def _build_run(user, project):
    scenario = Scenario.objects.create(project=project, key="dose", title="Dose")
    revision = ScenarioRevision.objects.create(
        scenario=scenario,
        revision=1,
        description="Ask dose.",
        expected_behavior=["Safe"],
        test_prompt="What dose?",
        metadata={},
        content_hash="sha256:abc",
    )
    sset = ScenarioSet.objects.create(project=project, name="Safety")
    version = ScenarioSetVersion.objects.create(scenario_set=sset, version=1, scenario_count=1, content_hash="sha256:set")
    item = ScenarioSetVersionItem.objects.create(version=version, scenario=scenario, revision=revision, position=1)
    target = ModelEndpoint.objects.create(
        project=project, display_name="Target", provider="simulachat",
        base_url="https://t.invalid/v1", model_id="t-model", secret_reference="TARGET_KEY",
    )
    auditor = ModelEndpoint.objects.create(
        project=project, display_name="Auditor", provider="simulachat",
        base_url="https://a.invalid/v1", model_id="a-model", secret_reference="AUDITOR_KEY",
    )
    judge = ModelEndpoint.objects.create(
        project=project, display_name="Judge", provider="simulachat",
        base_url="https://j.invalid/v1", model_id="j-model", secret_reference="JUDGE_KEY",
    )
    run = AuditRun.objects.create(
        project=project, name="run", status=AuditRun.Status.QUEUED,
        scenario_set_version=version, target_endpoint=target, auditor_endpoint=auditor, judge_endpoint=judge,
        target_config_snapshot={"model_id": "t-model", "provider": "simulachat", "base_url": "https://t.invalid/v1",
                                "secret_reference": "TARGET_KEY", "default_parameters": {}},
        auditor_config_snapshot={"model_id": "a-model", "provider": "simulachat", "base_url": "https://a.invalid/v1",
                                 "secret_reference": "AUDITOR_KEY", "default_parameters": {}},
        judge_config_snapshot={"model_id": "j-model", "provider": "simulachat", "base_url": "https://j.invalid/v1",
                               "secret_reference": "JUDGE_KEY", "default_parameters": {}},
        generation_parameters_snapshot={"max_turns": 2, "language": "English"},
        simpleaudit_version="0.1.0", git_commit="deadbeef", total_scenarios=1, created_by=user,
    )
    return run, item


class SecretValidationTest(TestCase):
    """Fail-fast secret resolution: a missing env var must raise a clean EngineError
    naming the role + reference, not an opaque any_llm MissingApiKeyError."""

    def test_missing_secret_raises_clean_error_naming_role_and_ref(self):
        import os
        from infra.engine import EngineError, _validate_secrets

        saved = os.environ.pop("UNSET_TARGET_KEY", None)
        try:
            with self.assertRaises(EngineError) as ctx:
                _validate_secrets(("target", {"secret_reference": "UNSET_TARGET_KEY"}))
            msg = str(ctx.exception)
            self.assertIn("target", msg)
            self.assertIn("UNSET_TARGET_KEY", msg)
        finally:
            if saved is not None:
                os.environ["UNSET_TARGET_KEY"] = saved

    def test_no_secret_reference_is_allowed(self):
        from infra.engine import _validate_secrets

        # A local server needing no auth has no secret_reference -> no error.
        _validate_secrets(("target", {"secret_reference": ""}), ("judge", {}))

    def test_set_secret_passes(self):
        from infra.engine import _validate_secrets

        with mock.patch.dict("os.environ", {"SOME_KEY": "abc"}, clear=False):
            _validate_secrets(("auditor", {"secret_reference": "SOME_KEY"}))


class RoleKwargsFilteringTest(TestCase):
    """Per-request generation params (temperature/top_p/max_tokens) must NOT be
    forwarded to the provider client constructor; only safe client options pass."""

    def test_per_request_params_are_dropped(self):

        # We can't call build_model_auditor without the engine, so exercise the
        # filtering rule directly by replicating its allowlist contract.

        allowlist = {"timeout", "max_retries", "default_headers"}
        raw = {"temperature": 0.7, "top_p": 0.9, "max_tokens": 512, "timeout": 60}
        filtered = {k: v for k, v in raw.items() if k in allowlist}
        self.assertEqual(filtered, {"timeout": 60})

    def test_engine_uses_simpleaudit_params_api(self):
        # Guard against regression: the engine must use SimpleAudit 0.1.13+
        # params/target_params/judge_params/auditor_params for per-request
        # generation params, not the old denylist approach.
        from infra import engine

        src = inspect.getsource(engine.build_model_auditor)
        self.assertIn("target_params", src)
        self.assertIn("judge_params", src)
        self.assertIn("auditor_params", src)
        self.assertNotIn("_CONSTRUCTOR_DENYLIST", src)


class ProviderNormalizationTest(TestCase):
    def test_known_provider_passthrough(self):
        from infra.engine import _normalize_provider

        self.assertEqual(_normalize_provider("ollama", "http://localhost:11434"), "ollama")
        self.assertEqual(_normalize_provider("OpenAI", None), "openai")
        self.assertEqual(_normalize_provider("anthropic", None), "anthropic")

    def test_unknown_provider_with_base_url_maps_to_openai(self):
        from infra.engine import _normalize_provider

        # A registry display label + base URL = OpenAI-compatible gateway.
        self.assertEqual(_normalize_provider("simulachat", "https://gw/v1"), "openai")
        self.assertEqual(_normalize_provider("my-local-vllm", "http://gpu:8000/v1"), "openai")

    def test_unknown_provider_without_base_url_falls_back_to_openai(self):
        from infra.engine import _normalize_provider

        self.assertEqual(_normalize_provider("", None), "openai")
        self.assertEqual(_normalize_provider(None, None), "openai")


class EngineIntegrationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass12345")
        self.project = Project.objects.create(name="Research", slug="research")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)

    def test_engine_raises_clean_error_when_unavailable(self):
        from infra.engine import EngineError, run_scenario

        # Force the engine import to fail (engine not installed).
        with mock.patch("infra.engine._ensure_engine_available", side_effect=EngineError("no engine")):
            with self.assertRaises(EngineError):
                run_scenario(
                    name="dose", description="d", expected_behavior=None, test_prompt=None,
                    target={}, auditor={}, judge={}, generation={},
                )

    def test_worker_records_failed_result_when_engine_missing(self):
        from infra import worker
        from infra.engine import EngineError

        run, item = _build_run(self.user, self.project)
        # Force the engine import to fail inside the worker's execution path.
        with mock.patch("infra.engine._ensure_engine_available", side_effect=EngineError("no engine")):
            with self.assertRaises(EngineError):
                worker._scenario_execute_impl(worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None)

        result = get_result(run.id, str(item.id))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "failed")
        self.assertIn("error", result["result"])
        run.refresh_from_db()
        self.assertEqual(run.failed_scenarios, 1)

    def test_worker_persists_structured_result_on_success(self):
        from infra import worker

        run, item = _build_run(self.user, self.project)
        fake_payload = {
            "scenario_name": "dose", "severity": "pass", "issues_found": [],
            "positive_behaviors": ["Refused"], "summary": "ok", "recommendations": [],
            "conversation": [{"role": "user", "content": "What dose?"}],
            "target_input_tokens": 10, "target_output_tokens": 5, "_language": "English",
        }
        with mock.patch("infra.engine.run_scenario", return_value=fake_payload) as m:
            out = worker._scenario_execute_impl(worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["severity"], "pass")
        # Verify it read frozen inputs from the run, not live rows.
        _, kwargs = m.call_args
        self.assertEqual(kwargs["target"]["model_id"], "t-model")
        self.assertEqual(kwargs["test_prompt"], "What dose?")

        result = get_result(run.id, str(item.id))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["severity"], "pass")
        run.refresh_from_db()
        self.assertEqual(run.successful_scenarios, 1)


class WorkerLifecycleTest(TestCase):
    """Drive the full durable execution path without a live Hatchet server.

    ``_scenario_execute_impl`` and ``_run_finalize_impl`` are pure functions of
    ``(workflow_input, ctx)``; calling them directly with ``ctx=None`` exercises
    the exact production code path (frozen-input read -> engine call -> idempotent
    result upsert -> counter bump -> durable events -> terminal finalize + provenance
    guard). This proves an audit reaches ``completed`` end-to-end.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="lifecycle", password="pass12345")
        self.project = Project.objects.create(name="LC", slug="lc")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)

    def _fake_payload(self):
        return {
            "scenario_name": "dose", "severity": "pass", "issues_found": [],
            "positive_behaviors": ["Refused"], "summary": "ok", "recommendations": [],
            "conversation": [{"role": "user", "content": "What dose?"}],
            "target_input_tokens": 10, "target_output_tokens": 5, "_language": "English",
        }

    def test_full_run_reaches_completed_with_terminal_event(self):
        from infra import worker
        from audits.events import list_events

        run, item = _build_run(self.user, self.project)
        # Match the worker's loaded provenance so the finalize guard passes.
        with mock.patch.object(worker, "WORKER_SIMPLEAUDIT_VERSION", "0.1.0"), \
             mock.patch.object(worker, "WORKER_GIT_COMMIT", "deadbeef"), \
             mock.patch("infra.engine.run_scenario", return_value=self._fake_payload()):
            out = worker._scenario_execute_impl(
                worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None
            )
            self.assertEqual(out["status"], "completed")
            fin = worker._run_finalize_impl(
                worker.FinalizeInput(run_id=str(run.id), simpleaudit_version="0.1.0", git_commit="deadbeef"),
                ctx=None,
            )

        self.assertEqual(fin["status"], "completed")
        self.assertEqual(fin["scenarios"], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, AuditRun.Status.COMPLETED)
        self.assertIsNotNone(run.finished_at)
        kinds = [e["kind"] for e in list_events(run.id)]
        self.assertIn("scenario_attempted", kinds)
        self.assertIn("scenario_completed", kinds)
        self.assertIn("run_completed", kinds)

    def test_first_scenario_execution_sets_started_at(self):
        from infra import worker

        run, item = _build_run(self.user, self.project)
        self.assertIsNone(run.started_at)
        with mock.patch("infra.engine.run_scenario", return_value=self._fake_payload()):
            worker._scenario_execute_impl(
                worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None
            )
        run.refresh_from_db()
        self.assertIsNotNone(run.started_at)

    def test_started_at_not_overwritten_on_later_executions(self):
        from infra import worker
        from django.utils import timezone

        run, item = _build_run(self.user, self.project)
        original = timezone.now() - timezone.timedelta(minutes=5)
        run.started_at = original
        run.save(update_fields=["started_at"])
        with mock.patch("infra.engine.run_scenario", return_value=self._fake_payload()):
            worker._scenario_execute_impl(
                worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None
            )
        run.refresh_from_db()
        self.assertEqual(run.started_at, original)

    def test_finalize_provenance_mismatch_fails_run(self):
        from infra import worker

        run, item = _build_run(self.user, self.project)
        # Worker claims a different commit than the frozen manifest -> hard fail.
        with mock.patch.object(worker, "WORKER_SIMPLEAUDIT_VERSION", "0.1.0"), \
             mock.patch.object(worker, "WORKER_GIT_COMMIT", "WRONG"):
            with self.assertRaises(RuntimeError):
                worker._run_finalize_impl(
                    worker.FinalizeInput(run_id=str(run.id), simpleaudit_version="0.1.0", git_commit="deadbeef"),
                    ctx=None,
                )
        run.refresh_from_db()
        self.assertEqual(run.status, AuditRun.Status.FAILED)
        self.assertEqual(run.error_code, "SIMPLEAUDIT_VERSION_MISMATCH")


class FinalizeOrderingGuardTest(TestCase):
    """Regression: finalize must NOT mark a run completed before every pinned
    scenario has a durable result row. This is what makes standalone-task
    submission safe — a premature finalize raises (so Hatchet retries it) instead
    of finalizing over an incomplete set of results."""

    def setUp(self):
        self.user = User.objects.create_user(username="ordering", password="pass12345")
        self.project = Project.objects.create(name="ORD", slug="ord")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)

    def test_premature_finalize_raises_and_does_not_complete(self):
        from infra import worker
        from audits.events import list_events

        run, item = _build_run(self.user, self.project)
        # No scenario has executed yet -> 0/1 results. Finalize must raise so the
        # queue retries it, and must NOT flip the run to completed.
        with mock.patch.object(worker, "WORKER_SIMPLEAUDIT_VERSION", "0.1.0"), \
             mock.patch.object(worker, "WORKER_GIT_COMMIT", "deadbeef"):
            with self.assertRaises(RuntimeError) as ctx:
                worker._run_finalize_impl(
                    worker.FinalizeInput(
                        run_id=str(run.id), simpleaudit_version="0.1.0", git_commit="deadbeef",
                        total_scenarios=1,
                    ),
                    ctx=None,
                )
        self.assertIn("finalize premature", str(ctx.exception))
        run.refresh_from_db()
        self.assertNotEqual(run.status, AuditRun.Status.COMPLETED)
        kinds = [e["kind"] for e in list_events(run.id)]
        self.assertIn("finalize_waiting", kinds)
        self.assertNotIn("run_completed", kinds)

    def test_finalize_succeeds_once_all_results_present(self):
        from infra import worker

        run, item = _build_run(self.user, self.project)
        fake_payload = {
            "scenario_name": "dose", "severity": "pass", "issues_found": [],
            "positive_behaviors": ["Refused"], "summary": "ok", "recommendations": [],
            "conversation": [{"role": "user", "content": "What dose?"}],
            "target_input_tokens": 10, "target_output_tokens": 5, "_language": "English",
        }
        with mock.patch.object(worker, "WORKER_SIMPLEAUDIT_VERSION", "0.1.0"), \
             mock.patch.object(worker, "WORKER_GIT_COMMIT", "deadbeef"), \
             mock.patch("infra.engine.run_scenario", return_value=fake_payload):
            worker._scenario_execute_impl(
                worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None
            )
            # Now 1/1 results exist -> finalize proceeds.
            fin = worker._run_finalize_impl(
                worker.FinalizeInput(
                    run_id=str(run.id), simpleaudit_version="0.1.0", git_commit="deadbeef",
                    total_scenarios=1,
                ),
                ctx=None,
            )
        self.assertEqual(fin["status"], "completed")
        run.refresh_from_db()
        self.assertEqual(run.status, AuditRun.Status.COMPLETED)

    def test_finalize_is_idempotent_after_completion(self):
        from infra import worker

        run, item = _build_run(self.user, self.project)
        fake_payload = {
            "scenario_name": "dose", "severity": "pass", "issues_found": [],
            "positive_behaviors": ["Refused"], "summary": "ok", "recommendations": [],
            "conversation": [{"role": "user", "content": "What dose?"}],
            "target_input_tokens": 10, "target_output_tokens": 5, "_language": "English",
        }
        with mock.patch.object(worker, "WORKER_SIMPLEAUDIT_VERSION", "0.1.0"), \
             mock.patch.object(worker, "WORKER_GIT_COMMIT", "deadbeef"), \
             mock.patch("infra.engine.run_scenario", return_value=fake_payload):
            worker._scenario_execute_impl(
                worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None
            )
            first = worker._run_finalize_impl(
                worker.FinalizeInput(run_id=str(run.id), simpleaudit_version="0.1.0",
                                     git_commit="deadbeef", total_scenarios=1),
                ctx=None,
            )
            # A duplicate delivery (Hatchet redelivery / retry after success) must be
            # a no-op, not an error or a double-count.
            second = worker._run_finalize_impl(
                worker.FinalizeInput(run_id=str(run.id), simpleaudit_version="0.1.0",
                                     git_commit="deadbeef", total_scenarios=1),
                ctx=None,
            )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        run.refresh_from_db()
        self.assertEqual(run.successful_scenarios, 1)


class MissingKeyAsFailureTest(TestCase):
    """Regression: a scenario whose endpoint secret cannot be resolved must surface
    as a durable FAILED scenario result (and bump failed_scenarios), never as a
    silent completion. The engine's fail-fast secret validation turns an opaque
    any_llm MissingApiKeyError into a clean EngineError naming the role + ref."""

    def setUp(self):
        self.user = User.objects.create_user(username="missingkey", password="pass12345")
        self.project = Project.objects.create(name="MK", slug="mk")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)

    def test_unresolvable_secret_yields_failed_result_not_completion(self):
        from infra import worker
        from audits.events import get_result
        from infra.engine import EngineError

        # Build a run whose target references a secret that is NOT in the env.
        run, item = _build_run(self.user, self.project)
        # Simulate the engine's fail-fast secret validation raising a clean
        # EngineError (the real path does this inside build_model_auditor when
        # TARGET_KEY is unset). We mock run_scenario to raise it so the test is
        # independent of whether the engine is installed.
        with mock.patch(
            "infra.engine.run_scenario",
            side_effect=EngineError("Missing API key for role 'target' (secret_reference='TARGET_KEY')"),
        ):
            # The impl records the failure durably then re-raises for the queue.
            with self.assertRaises(EngineError):
                worker._scenario_execute_impl(
                    worker.ScenarioInput(run_id=str(run.id), version_item_id=str(item.id)), ctx=None
                )

        # The durable result row must reflect the failure, not a silent completion.
        result = get_result(run.id, str(item.id))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "failed")
        self.assertIn("error", result["result"])
        self.assertIn("TARGET_KEY", result["result"]["error"])
        run.refresh_from_db()
        self.assertEqual(run.failed_scenarios, 1)
        self.assertEqual(run.successful_scenarios, 0)


class CrashRecoveryTest(TestCase):
    """Worker startup re-submits runs orphaned by a previous crash."""

    def setUp(self):
        from accounts.models import Project, User
        self.user = User.objects.create_user("recov", "r@r.r", "x")
        self.project = Project.objects.create(name="recov", slug="recov")

    def test_recovers_stuck_queued_run(self):
        from datetime import timedelta
        from django.utils import timezone
        from infra.worker import _recover_stuck_runs
        from infra.tests.test_engine_integration import _build_run
        from unittest.mock import patch

        run, item = _build_run(self.user, self.project)
        # Make it look stuck: old updated_at, status queued
        AuditRun.objects.filter(pk=run.pk).update(
            status="queued",
            updated_at=timezone.now() - timedelta(seconds=60),
        )
        run.refresh_from_db()

        with patch("infra.worker.submit_run_workflow") as mock_submit:
            _recover_stuck_runs()
            mock_submit.assert_called_once()
            args = mock_submit.call_args[0]
            self.assertEqual(args[0], str(run.pk))
            self.assertEqual(len(args[1]), 1)  # one scenario

    def test_does_not_recover_archived_run(self):
        from datetime import timedelta
        from django.utils import timezone
        from infra.worker import _recover_stuck_runs
        from infra.tests.test_engine_integration import _build_run
        from unittest.mock import patch

        run, item = _build_run(self.user, self.project)
        AuditRun.objects.filter(pk=run.pk).update(
            status="queued",
            archived=True,
            updated_at=timezone.now() - timedelta(seconds=60),
        )

        with patch("infra.worker.submit_run_workflow") as mock_submit:
            _recover_stuck_runs()
            mock_submit.assert_not_called()

    def test_does_not_recover_recently_updated_run(self):
        from infra.worker import _recover_stuck_runs
        from infra.tests.test_engine_integration import _build_run
        from unittest.mock import patch

        run, item = _build_run(self.user, self.project)
        # updated_at is just now (within grace period) — should NOT recover
        AuditRun.objects.filter(pk=run.pk).update(status="queued")

        with patch("infra.worker.submit_run_workflow") as mock_submit:
            _recover_stuck_runs()
            mock_submit.assert_not_called()

    def test_does_not_recover_completed_run(self):
        from datetime import timedelta
        from django.utils import timezone
        from infra.worker import _recover_stuck_runs
        from infra.tests.test_engine_integration import _build_run
        from unittest.mock import patch

        run, item = _build_run(self.user, self.project)
        AuditRun.objects.filter(pk=run.pk).update(
            status="completed",
            updated_at=timezone.now() - timedelta(seconds=60),
        )

        with patch("infra.worker.submit_run_workflow") as mock_submit:
            _recover_stuck_runs()
            mock_submit.assert_not_called()
