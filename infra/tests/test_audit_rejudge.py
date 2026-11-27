"""Rejudge: a new run that grades a finished run's stored transcripts with another judge.

Only the judge model client is faked. The rest is the real path: the service
that freezes the run, the worker's scenario task and ``simpleaudit.rejudge``.
"""
import json
from types import SimpleNamespace
from unittest import mock

from django.test import Client, TestCase

from accounts.models import Project, ProjectMembership, User
from audits.events import ScenarioResult, get_result, list_events
from audits.models import AuditRun
from audits.services import create_rejudge_run, rejudge_block_reason
from infra import worker
from infra.exceptions import StableAPIError
from infra.tests.test_engine_integration import _build_run
from judges.models import Judge, JudgeVersion
from model_registry.models import ModelConnection, RegisteredModel
from scenarios.models import Scenario, ScenarioRevision, ScenarioSetVersionItem

TRANSCRIPT = [{"role": "user", "content": "What dose?"}, {"role": "assistant", "content": "Take two."}]


def _stored(severity="pass", **extra) -> dict:
    """One scenario's stored result, as the worker writes it for a finished run."""
    return {
        "scenario_name": "dose", "scenario_description": "Ask dose.", "conversation": TRANSCRIPT,
        "severity": severity, "issues_found": [], "positive_behaviors": [], "summary": "old verdict",
        "recommendations": [], "expected_behavior": ["Safe"],
        "judgment": {"severity": severity, "summary": "old verdict"},
        "target_input_tokens": 10, "target_output_tokens": 5, "auditor_input_tokens": 3,
        "auditor_output_tokens": 2, "judge_input_tokens": 7, "judge_output_tokens": 1, **extra,
    }


SPAN = {
    "span_id": "s1", "trace_id": "t1", "name": "execute_tool lookup_order", "kind": "TOOL",
    "start_time": 1.0, "end_time": 1.2,
    "attributes": {
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup_order",
        "gen_ai.tool.call.result": "order 4417 shipped on 3 May",
    },
}
TRACE = {"trace_ids": ["t1"], "all_spans": [SPAN], "selected_spans": [SPAN]}


def _traced(severity="pass", **extra) -> dict:
    """A traced run's stored result: the transcript and the trace, as ``run_scenario`` keeps them."""
    result = _stored(severity, trace_ids=["t1"], trace_evidence=TRACE, **extra)
    result["judgment"]["evidence_spans"] = [SPAN]
    return result


class FakeJudge:
    """Stands in for the judge's model client: records its calls and returns one verdict."""

    def __init__(self, severity="high", error=None, reply=None):
        self.calls, self.error = [], error
        self.reply = reply or json.dumps({
            "severity": severity, "issues_found": ["Gave a dose"], "positive_behaviors": [],
            "summary": "new verdict", "recommendations": [],
        })

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))],
            usage=SimpleNamespace(prompt_tokens=40, completion_tokens=9),
        )

    def installed(self):
        return mock.patch("simpleaudit.model_auditor.ModelAuditor._create_anyllm_client", return_value=self)


class RejudgeTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rejudger", password="pass12345")
        self.project = Project.objects.create(name="RJ", slug="rj")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.source, self.item = _build_run(self.user, self.project)
        self.source.status = AuditRun.Status.COMPLETED
        # Fast engine retries for the tests that make the judge fail.
        self.source.generation_parameters_snapshot = {"max_turns": 2, "language": "English", "retry_backoff": 0.001}
        self.source.save()
        self._source_result(_stored("pass"))
        self.judge = JudgeVersion.objects.create(
            judge=Judge.objects.create(project=self.project, name="Harm judge"), version=1, base="harm",
        )
        connection = ModelConnection.objects.create(
            project=self.project, name="New judge conn", provider="openai", base_url="https://nj.invalid/v1",
        )
        self.judge_model = RegisteredModel.objects.create(
            connection=connection, project=self.project, display_name="New judge", model_id="nj-model",
            default_parameters={"temperature": 0, "max_tokens": 512},
        )

    def _source_result(self, result, status="completed", attempts=1):
        """Set what the source run stored for the scenario (a passing result by default)."""
        ScenarioResult.objects.filter(run_id=self.source.id).delete()
        return ScenarioResult.objects.create(
            run_id=self.source.id, version_item_id=str(self.item.id), status=status, attempts=attempts, result=result,
        )

    def _rejudge(self, **kwargs) -> AuditRun:
        return create_rejudge_run(
            source=self.source, user=self.user,
            judge=kwargs.pop("judge", self.judge), judge_model=kwargs.pop("judge_model", self.judge_model), **kwargs,
        )

    def _execute(self, run: AuditRun, item=None) -> dict:
        return worker._scenario_execute_impl(
            worker.ScenarioInput(run_id=str(run.id), version_item_id=str((item or self.item).id)), ctx=None
        )


class RejudgeRunTests(RejudgeTestCase):
    def test_new_run_keeps_the_source_inputs_and_freezes_its_own_judge(self):
        run = self._rejudge()

        self.assertEqual(run.status, AuditRun.Status.QUEUED)
        self.assertEqual(run.runtime_metadata["rejudged_from"], {"run_id": self.source.id, "name": "run"})
        self.assertEqual(run.name, "run · rejudged with Harm judge")
        self.assertEqual(run.scenario_set_version_id, self.source.scenario_set_version_id)
        self.assertEqual(run.target_config_snapshot, self.source.target_config_snapshot)
        self.assertEqual(run.auditor_config_snapshot, self.source.auditor_config_snapshot)
        self.assertEqual(run.generation_parameters_snapshot, self.source.generation_parameters_snapshot)
        self.assertEqual(run.judge_version_id, self.judge.id)
        self.assertEqual(run.judge_config_snapshot["model_id"], "nj-model")
        self.assertEqual(run.judge_config_snapshot["judge"]["name"], "Harm judge")
        self.assertEqual(run.trace_config, {})
        self.assertEqual(run.total_scenarios, 1)

    def test_worker_grades_the_stored_transcript_with_the_new_judge(self):
        run = self._rejudge()
        judge = FakeJudge(severity="high")

        with judge.installed():
            out = self._execute(run)

        self.assertEqual(out, {"status": "completed", "attempt": 1, "severity": "high"})
        result = get_result(run.id, str(self.item.id))["result"]
        self.assertEqual(result["conversation"], TRANSCRIPT)
        self.assertEqual((result["severity"], result["summary"]), ("high", "new verdict"))
        # Target and auditor tokens are the source run's; judge tokens are this run's.
        self.assertEqual((result["target_input_tokens"], result["auditor_output_tokens"]), (10, 2))
        self.assertEqual((result["judge_input_tokens"], result["judge_output_tokens"]), (40, 9))
        self.assertNotIn("failure", result)

        # One call: to the new judge model, with its frozen request params and
        # the frozen prompt of the judge this run picked.
        self.assertEqual(len(judge.calls), 1)
        call = judge.calls[0]
        self.assertEqual((call["model"], call["temperature"], call["max_tokens"]), ("nj-model", 0, 512))
        self.assertEqual(call["messages"][0]["content"], run.judge_config_snapshot["judge"]["judge_prompt"])
        self.assertIn("Take two.", call["messages"][-1]["content"])
        self.assertNotIn("OBSERVED INTERNAL TRACES", call["messages"][-1]["content"])

        run.refresh_from_db()
        self.assertEqual(run.status, AuditRun.Status.COMPLETED)
        stages = [e["payload"]["stage"] for e in list_events(run.id) if e["kind"] == "run_stage"]
        self.assertIn("judging", stages)
        self.assertNotIn("target_execution", stages)
        # The source run is untouched.
        self.assertEqual(get_result(self.source.id, str(self.item.id))["result"]["severity"], "pass")

    def test_every_repetition_is_regraded_and_the_aggregate_recomputed(self):
        self._source_result({
            "reps": [_stored("pass", _rep_index=0), _stored("pass", _rep_index=1)],
            "aggregated_severity": "pass", "agreement_rate": 1.0, "severity_distribution": {"pass": 2},
            "n_repetitions": 2,
        })
        run = self._rejudge()
        judge = FakeJudge(severity="critical")

        with judge.installed():
            out = self._execute(run)

        self.assertEqual(out["severity"], "critical")
        self.assertEqual(len(judge.calls), 2)
        result = get_result(run.id, str(self.item.id))["result"]
        self.assertEqual([rep["severity"] for rep in result["reps"]], ["critical", "critical"])
        self.assertEqual([rep["_rep_index"] for rep in result["reps"]], [0, 1])
        self.assertEqual(result["aggregated_severity"], "critical")
        self.assertEqual(result["severity_distribution"], {"critical": 2})
        self.assertEqual(result["n_repetitions"], 2)

    def test_scenario_the_source_never_completed_stays_failed_without_a_judge_call(self):
        failure = {"role": "target", "kind": "auth", "message": "AuthenticationError: bad key"}
        self._source_result({"error": "AuthenticationError: bad key", "failure": failure}, status="failed", attempts=3)
        run = self._rejudge()
        judge = FakeJudge()

        # The engine says so, and the worker makes it final at once (no retry raised).
        with judge.installed(), self.assertWarnsRegex(UserWarning, "no transcript"):
            out = self._execute(run)

        self.assertEqual(out["status"], "failed")
        self.assertEqual(judge.calls, [])
        row = get_result(run.id, str(self.item.id))
        self.assertEqual((row["status"], row["attempts"]), ("failed", worker.MAX_SCENARIO_ATTEMPTS))
        self.assertEqual(row["result"]["failure"], {**failure, "retryable": False})
        run.refresh_from_db()
        self.assertEqual(run.status, AuditRun.Status.FAILED)

    def test_scenario_the_source_never_ran_is_skipped(self):
        other = Scenario.objects.create(project=self.project, key="never-ran", title="Never ran")
        revision = ScenarioRevision.objects.create(
            scenario=other, revision=1, description="Not run by the source.", expected_behavior=["Safe"],
            metadata={}, content_hash="sha256:def",
        )
        never_ran = ScenarioSetVersionItem.objects.create(
            version=self.source.scenario_set_version, scenario=other, revision=revision, position=2,
        )
        run = self._rejudge()
        self.assertEqual(run.total_scenarios, 1)   # the source's results, not the set's two scenarios
        judge = FakeJudge(severity="low")

        with judge.installed():
            self.assertEqual(self._execute(run, never_ran), {"status": "skipped_no_source"})
            self.assertIsNone(get_result(run.id, str(never_ran.id)))
            self.assertEqual(list_events(run.id), [])
            self._execute(run)

        self.assertEqual(len(judge.calls), 1)
        run.refresh_from_db()
        self.assertEqual((run.status, run.completed_scenarios), (AuditRun.Status.COMPLETED, 1))

    def test_judge_answer_without_a_verdict_is_retried_like_in_a_live_run(self):
        run = self._rejudge()

        with FakeJudge(reply="I cannot grade this.").installed(), \
                self.assertRaisesRegex(worker.ScenarioAttemptFailed, "attempt 1 failed"):
            self._execute(run)

        row = get_result(run.id, str(self.item.id))
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))   # provisional
        self.assertEqual(row["result"]["failure"]["role"], "system")
        self.assertIsNot(row["result"]["failure"].get("retryable"), False)

    def test_failing_judge_call_is_attributed_to_the_judge_and_retried(self):
        run = self._rejudge()
        judge = FakeJudge(error=RuntimeError("RateLimitError: Error code: 429"))

        with judge.installed(), self.assertRaisesRegex(worker.ScenarioAttemptFailed, "attempt 1 failed"):
            self._execute(run)

        row = get_result(run.id, str(self.item.id))
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))   # provisional
        failure = row["result"]["failure"]
        self.assertEqual((failure["role"], failure["kind"], failure["model"]), ("judge", "rate_limit", "nj-model"))
        self.assertEqual(row["result"]["conversation"], TRANSCRIPT)

    def test_source_whose_judge_failed_can_be_rejudged(self):
        failed = _stored("ERROR")
        failed["judgment"] = {"severity": "ERROR", "error": "APIError: 503", "error_phase": "judge"}
        failed["failure"] = {"role": "judge", "kind": "unavailable", "message": "APIError: 503"}
        self._source_result(failed, status="failed", attempts=3)
        self.source.status = AuditRun.Status.FAILED
        self.source.save(update_fields=["status"])
        run = self._rejudge()

        with FakeJudge(severity="pass").installed():
            out = self._execute(run)

        self.assertEqual(out["status"], "completed")
        result = get_result(run.id, str(self.item.id))["result"]
        self.assertEqual(result["severity"], "pass")
        self.assertNotIn("failure", result)


class RejudgeTracedRunTests(RejudgeTestCase):
    """A traced run's judge also read the target's trace; so does the judge of a rejudge."""

    def setUp(self):
        super().setUp()
        self.source.trace_config = {"mode": "studio", "target_id": "agent_1"}
        self.source.save(update_fields=["trace_config"])

    def test_new_run_is_traced_like_its_source(self):
        self.assertEqual(rejudge_block_reason(self.source), "")
        self.assertEqual(self._rejudge().trace_config, {"mode": "studio", "target_id": "agent_1"})

    def test_result_is_graded_with_its_trace_and_keeps_it(self):
        self._source_result(_traced())
        run = self._rejudge()
        judge = FakeJudge(severity="high")

        # No trace is acquired, so nothing is said about the target's OTLP credential.
        with judge.installed(), self.assertNoLogs("simpleaudit.tracing", level="WARNING"):
            self._execute(run)

        prompt = judge.calls[0]["messages"][-1]["content"]
        self.assertIn("OBSERVED INTERNAL TRACES", prompt)
        self.assertIn("order 4417 shipped on 3 May", prompt)
        result = get_result(run.id, str(self.item.id))["result"]
        self.assertEqual((result["severity"], result["summary"]), ("high", "new verdict"))
        self.assertEqual((result["trace_evidence"], result["trace_ids"]), (TRACE, ["t1"]))
        self.assertEqual(result["judgment"]["evidence_spans"], [SPAN])
        self.assertEqual(result["judgment"]["final_response"], "Take two.")

    def test_rejudged_run_can_be_rejudged_with_the_trace_again(self):
        self._source_result(_traced())
        first = self._rejudge()
        with FakeJudge().installed():
            self._execute(first)
        first.refresh_from_db()
        judge = FakeJudge(severity="low")

        second = create_rejudge_run(source=first, user=self.user, judge=self.judge, judge_model=self.judge_model)
        with judge.installed():
            self._execute(second)

        self.assertIn("order 4417 shipped on 3 May", judge.calls[0]["messages"][-1]["content"])
        result = get_result(second.id, str(self.item.id))["result"]
        self.assertEqual((result["severity"], result["trace_evidence"]), ("low", TRACE))

    def test_repetitions_get_the_trace_for_an_agentic_scenario_only(self):
        # As in a live run: run_scenario_repeated has no evidence resolver otherwise.
        self._source_result({
            "reps": [_traced(_rep_index=0), _traced(_rep_index=1)],
            "aggregated_severity": "pass", "agreement_rate": 1.0, "severity_distribution": {"pass": 2},
            "n_repetitions": 2,
        })
        judge = FakeJudge()

        with judge.installed():
            self._execute(self._rejudge())
        ScenarioRevision.objects.filter(pk=self.item.revision_id).update(metadata={"agentic": {"schema_version": 2}})
        with judge.installed():
            run = self._rejudge()
            self._execute(run)

        traced = ["OBSERVED INTERNAL TRACES" in call["messages"][-1]["content"] for call in judge.calls]
        self.assertEqual(traced, [False, False, True, True])
        reps = get_result(run.id, str(self.item.id))["result"]["reps"]
        self.assertEqual([rep["trace_evidence"] for rep in reps], [TRACE, TRACE])

    def test_agentic_checks_are_shown_and_the_new_judge_gives_its_own_semantic_verdict(self):
        ScenarioRevision.objects.filter(pk=self.item.revision_id).update(metadata={"agentic": {
            "schema_version": 2, "judge_guidance": "A wrong tool call fails the scenario.",
            "semantic_judge": {"enabled": True, "criteria": ["grounding"]},
        }})
        # The source's checks passed and its judge failed the semantic criterion.
        self._source_result(_traced(agentic_evaluation={
            "status": "FAIL", "reason": "Semantic check failed",
            "verdict": {"status": "FAIL", "reason": "Semantic check failed"},
            "checks": [{"id": "rule.0.check", "category": "rule", "status": "PASS", "summary": "1 matching tool unit(s)"}],
            "trace_completeness": {"complete": True, "span_count": 1},
            "semantic_judgement": {"status": "FAIL", "dimensions": {
                "grounding": {"status": "FAIL", "rationale": "source judge: invented a date", "span_ids": ["s1"]},
            }},
        }))
        run = self._rejudge()
        judge, semantic_prompts = FakeJudge(severity="pass"), []

        def semantic_judge(prompt: str) -> str:
            semantic_prompts.append(prompt)
            return json.dumps({"dimensions": {
                "grounding": {"status": "PASS", "rationale": "new judge: matches the tool result", "span_ids": ["s1"]},
            }})

        with judge.installed(), mock.patch.object(worker, "_build_semantic_judge_llm", return_value=semantic_judge):
            self._execute(run)

        # The judge reads the checks as they stood before the source judge's
        # semantic verdict, with the scenario's guidance.
        prompt = judge.calls[0]["messages"][-1]["content"]
        self.assertIn("order 4417 shipped on 3 May", prompt)
        self.assertIn("rule.0.check: PASS", prompt)
        self.assertIn("Agentic pre-assessment: INCONCLUSIVE", prompt)
        self.assertIn("INSTRUCTION: A wrong tool call fails the scenario.", prompt)
        self.assertNotIn("source judge", prompt)
        self.assertNotIn("Semantic check failed", prompt)

        evaluation = get_result(run.id, str(self.item.id))["result"]["agentic_evaluation"]
        self.assertEqual(len(semantic_prompts), 1)
        self.assertEqual(evaluation["semantic_judgement"]["dimensions"]["grounding"]["status"], "PASS")
        self.assertEqual(evaluation["status"], "PASS")
        self.assertEqual([check["id"] for check in evaluation["checks"]], ["rule.0.check"])
        # The source keeps its own judge's verdict.
        source = get_result(self.source.id, str(self.item.id))["result"]["agentic_evaluation"]
        self.assertEqual(source["semantic_judgement"]["dimensions"]["grounding"]["status"], "FAIL")


class RejudgeRulesTests(RejudgeTestCase):
    def test_run_without_results_cannot_be_rejudged(self):
        ScenarioResult.objects.filter(run_id=self.source.id).delete()
        self.assertIn("no results", rejudge_block_reason(self.source))
        self._refused("run_not_rejudgeable")

    def _refused(self, code, **kwargs):
        with self.assertRaises(StableAPIError) as raised:
            self._rejudge(**kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(AuditRun.objects.count(), 1)

    def test_running_run_cannot_be_rejudged(self):
        self.source.status = AuditRun.Status.JUDGING
        self.source.save(update_fields=["status"])
        self.assertIn("still in progress", rejudge_block_reason(self.source))
        self._refused("run_not_rejudgeable")

    def test_decision_target_cannot_be_rejudged(self):
        self.source.target_config_snapshot = {**self.source.target_config_snapshot, "capabilities": {"decision": True}}
        self.source.save(update_fields=["target_config_snapshot"])
        self.assertIn("decision target", rejudge_block_reason(self.source))
        self._refused("run_not_rejudgeable")

    def test_judges_that_need_the_live_run_and_decision_models_are_refused(self):
        for base, output in (("decision_safety", "decision"), ("choice_match", "binary")):
            needs_live_run = JudgeVersion.objects.create(
                judge=Judge.objects.create(project=self.project, name=base), version=1, base=base, output=output,
            )
            self._refused("judge_model_mismatch", judge=needs_live_run)
        self.judge_model.capabilities = {"decision": True}
        self.judge_model.save(update_fields=["capabilities"])
        self._refused("judge_model_mismatch")

    def test_judge_and_model_must_belong_to_the_workspace(self):
        other = Project.objects.create(name="Other", slug="other")
        foreign_judge = JudgeVersion.objects.create(
            judge=Judge.objects.create(project=other, name="Foreign"), version=1, base="safety",
        )
        self._refused("cross_project_input", judge=foreign_judge)
        self.judge_model.enabled = False
        self.judge_model.save(update_fields=["enabled"])
        self._refused("model_unavailable")

    def test_viewer_cannot_rejudge(self):
        ProjectMembership.objects.filter(user=self.user).update(role=ProjectMembership.Role.VIEWER)
        with self.assertRaises(StableAPIError):
            self._rejudge()
        self.assertEqual(AuditRun.objects.count(), 1)


class RejudgeUiTests(RejudgeTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="rejudger", password="pass12345")
        self.url = f"/runs/{self.source.id}/rejudge/"

    def test_run_page_offers_rejudge_with_judges_and_models(self):
        JudgeVersion.objects.create(
            judge=Judge.objects.create(project=self.project, name="Code graded"), version=1, base="choice_match",
        )
        html = self.client.get(f"/runs/{self.source.id}/").content.decode()
        self.assertIn(f'action="{self.url}"', html)
        self.assertIn("Harm judge v1", html)
        self.assertIn("New judge", html)
        self.assertNotIn("Code graded v1", html)   # Choice Match cannot grade a stored transcript

    def test_run_page_says_why_a_run_cannot_be_rejudged(self):
        self.source.status = AuditRun.Status.JUDGING
        self.source.save(update_fields=["status"])
        html = self.client.get(f"/runs/{self.source.id}/").content.decode()
        self.assertNotIn(self.url, html)
        self.assertIn("This run is still in progress", html)
        self.assertIn("Rejudge is not available for this run", html)

    def test_post_creates_and_submits_the_run_then_opens_it(self):
        with mock.patch("infra.ui.submit_audit_run") as submit:
            response = self.client.post(self.url, {"judge": self.judge.id, "judge_model": self.judge_model.id})

        run = AuditRun.objects.exclude(pk=self.source.pk).get()
        self.assertRedirects(response, f"/runs/{run.id}/", fetch_redirect_response=False)
        submit.assert_called_once_with(run)
        self.assertEqual(run.runtime_metadata["rejudged_from"]["run_id"], self.source.id)
        html = self.client.get(f"/runs/{run.id}/").content.decode()
        self.assertIn(f"Rejudged from #{self.source.id}", html)
        self.assertIn(f'href="/runs/{self.source.id}/"', html)

    def test_post_without_a_valid_choice_creates_nothing(self):
        with mock.patch("infra.ui.submit_audit_run") as submit:
            response = self.client.post(self.url, {"judge": "nope", "judge_model": self.judge_model.id})
        self.assertRedirects(response, f"/runs/{self.source.id}/", fetch_redirect_response=False)
        submit.assert_not_called()
        self.assertEqual(AuditRun.objects.count(), 1)

    def test_viewer_gets_no_rejudge_form_and_cannot_post(self):
        ProjectMembership.objects.filter(user=self.user).update(role=ProjectMembership.Role.VIEWER)
        self.assertNotIn(self.url, self.client.get(f"/runs/{self.source.id}/").content.decode())
        with mock.patch("infra.ui.submit_audit_run") as submit:
            self.client.post(self.url, {"judge": self.judge.id, "judge_model": self.judge_model.id})
        submit.assert_not_called()
        self.assertEqual(AuditRun.objects.count(), 1)
