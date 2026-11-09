"""Which role failed a scenario: classification, worker wiring and UI.

Role is the only required field of the stored ``failure`` object; kind,
http_status, endpoint, model and retryable are best effort.
"""
import json

import pytest
from django.test import Client, TestCase

from audits.failures import (
    PhaseTracker,
    attach_failures,
    build_failure,
    count_by_role,
    engine_error_failure,
    result_failure,
    summary_text,
)
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    ScenarioResultFactory,
    UserFactory,
)
from infra.tests.test_agentic_semantic_judge_lane import (
    _rep,
    _semantic_metadata,
    _spans,
    _workflow_input,
)
from infra.tests.test_agentic_semantic_judge_lane import (
    patched_worker as _patched_worker,
)
from infra.worker import _scenario_execute_impl

patched_worker = _patched_worker  # re-exported so pytest finds the fixture

# What each failure looks like as the engine records it: "<Type>: <message>".
ERRORS = {
    "429": ("RateLimitError: Error code: 429 - Too Many Requests", "rate_limit", 429),
    "5xx": ("APIError: The language model provider is unavailable (status 503)", "unavailable", 503),
    "timeout": ("APITimeoutError: Request timed out.", "timeout", None),
    "connection": ("APIConnectionError: Connection error.", "unavailable", None),
}
SNAPSHOTS = {
    "target": {"base_url": "http://target/v1", "model_id": ""},
    "auditor": {"base_url": "http://auditor/v1", "model_id": "aud-1"},
    "judge": {"base_url": "http://judge/v1", "model_id": "jud-1"},
}


def _error_result(message, phase):
    return {
        "severity": "ERROR",
        "summary": f"Scenario did not complete due to an error: {message}",
        "judgment": {"severity": "ERROR", "error": message, "error_phase": phase},
    }


def _tracker_for(role):
    """A tracker whose engine callbacks stopped right before ``role`` ran."""
    tracker = PhaseTracker()
    if role == "target":
        tracker.note(0, 0, 3, "auditor")
    elif role == "auditor":
        tracker.note(0, 0, 3, "target")
    return tracker


@pytest.mark.parametrize("role", ["target", "auditor", "judge"])
@pytest.mark.parametrize("name", list(ERRORS))
def test_role_and_kind_for_each_failure(role, name):
    message, kind, status = ERRORS[name]
    payload = _error_result(message, "judge" if role == "judge" else "conversation")
    failure = attach_failures(payload, _tracker_for(role), SNAPSHOTS, attempt=2)
    assert failure["role"] == role
    assert failure["kind"] == kind
    assert failure.get("http_status") == status
    assert failure["message"] == message          # raw text kept
    assert failure["attempt"] == 2
    assert failure["endpoint"] == SNAPSHOTS[role]["base_url"]
    assert payload["failure"] is failure


def test_model_only_when_known_never_assumed():
    payload = _error_result(ERRORS["429"][0], "conversation")
    failure = attach_failures(payload, _tracker_for("target"), SNAPSHOTS)
    assert "model" not in failure and failure["endpoint"] == "http://target/v1"
    judge = attach_failures(_error_result("x", "judge"), PhaseTracker(), SNAPSHOTS)
    assert judge["model"] == "jud-1"


def test_role_is_the_only_required_field():
    failure = build_failure("target", "something odd")
    assert failure == {"role": "target", "kind": "unknown", "message": "something odd"}
    assert build_failure("nonsense", "x")["role"] == "system"


def test_fixed_test_prompt_skips_auditor():
    payload = _error_result("APIConnectionError: Connection error.", "conversation")
    failure = attach_failures(payload, PhaseTracker(), SNAPSHOTS, probe_is_fixed=True)
    assert failure["role"] == "target"


def test_repeated_result_tags_each_error_rep():
    tracker = PhaseTracker()
    tracker.note(1, 0, 3, "auditor")
    payload = {"reps": [{"severity": "pass"}, _error_result(ERRORS["5xx"][0], "conversation")]}
    attach_failures(payload, tracker, SNAPSHOTS)
    assert "failure" not in payload["reps"][0]
    assert payload["reps"][1]["failure"]["role"] == "target"
    assert result_failure(payload)["role"] == "target"


@pytest.mark.parametrize("name", list(ERRORS))
def test_engine_crash_is_system(name):
    failure = engine_error_failure(f"Scenario execution crashed: {ERRORS[name][0]}", SNAPSHOTS)
    assert failure["role"] == "system"


def test_missing_secret_names_the_endpoint_role():
    failure = engine_error_failure(
        "Secret for judge endpoint is not set: environment variable 'K' is missing or empty.", SNAPSHOTS
    )
    assert (failure["role"], failure["kind"], failure["retryable"]) == ("judge", "config", False)


def test_summary_and_counts():
    results = [{"failure": {"role": "target"}}] * 44 + [{"failure": {"role": "judge"}}] * 3 + [{"error": "old"}]
    counts = count_by_role(results)
    assert counts == {"target": 44, "judge": 3}
    assert summary_text(counts, 47) == "47 failed: 44 target, 3 judge"
    assert summary_text({}, 5) == "5 failed"


# --- worker --------------------------------------------------------------------

@pytest.fixture
def engine_patch(monkeypatch, patched_worker):
    import infra.engine as engine_module

    def drive(phases, result):
        def fake(**kwargs):
            for turn, role in phases:
                kwargs["on_turn"](turn, 3, role)
            return result
        monkeypatch.setattr(engine_module, "run_scenario", fake)
    return drive


@pytest.mark.parametrize(
    ("phases", "phase", "role"),
    [
        ([(0, "auditor")], "conversation", "target"),
        ([(0, "auditor"), (0, "target")], "conversation", "auditor"),
        ([(0, "auditor"), (0, "target"), (2, "target")], "judge", "judge"),
    ],
)
def test_worker_stores_role_and_counts_it(engine_patch, patched_worker, phases, phase, role):
    engine_patch(phases, _error_result(ERRORS["429"][0], phase))
    with pytest.raises(Exception, match="attempt 1 failed"):   # provisional: Hatchet retries
        _scenario_execute_impl(_workflow_input(), ctx=None)
    stored = patched_worker["upserts"][0][1]
    assert stored["status"] == "failed"
    assert stored["result"]["failure"]["role"] == role
    event = next(e for e in patched_worker["events"] if e[0][2] == "scenario_failed")
    assert event[0][3]["failure"]["role"] == role


def test_judge_endpoint_failure_is_failed_not_uncertain(monkeypatch, patched_worker):
    """The semantic judge's endpoint raising must not surface as UNCERTAIN."""
    import infra.worker as worker_module

    def broken(prompt):
        raise RuntimeError("RateLimitError: Error code: 429")

    monkeypatch.setattr(worker_module, "_build_semantic_judge_llm", lambda snap: broken)
    patched_worker["revision_metadata"] = _semantic_metadata()
    patched_worker["rep"] = _rep(trace_evidence={"all_spans": _spans()})

    with pytest.raises(Exception, match="attempt 1 failed"):
        _scenario_execute_impl(_workflow_input(), ctx=None)

    stored = patched_worker["upserts"][0][1]
    assert stored["status"] == "failed"
    result = stored["result"]
    assert result["severity"] == "ERROR"
    assert result["failure"]["role"] == "judge" and result["failure"]["kind"] == "rate_limit"
    assert result["failure"]["http_status"] == 429


# --- UI ------------------------------------------------------------------------

class FailureUiTests(TestCase):
    def setUp(self):
        user = UserFactory()
        user.set_password("pw")
        user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=user, project=self.project, role="admin")
        self.client = Client()
        self.client.login(username=user.username, password="pw")
        self.run = AuditRunFactory(project=self.project, status="completed")

    def _fail(self, role, kind="rate_limit", message="RateLimitError: 429", **extra):
        failure = build_failure(role, message, kind=kind, **extra)
        return ScenarioResultFactory(
            run_id=self.run.id, status="failed",
            result={"severity": "ERROR", "summary": message, "failure": failure,
                    "judgment": {"error": message}},
        )

    def test_summary_badges_and_filter_buttons(self):
        for _ in range(2):
            self._fail("target")
        self._fail("judge", kind="unavailable", message="APIError: provider is unavailable")
        html = self.client.get(f"/runs/{self.run.id}/results-fragment/").content.decode()
        assert "3 failed: 2 target, 1 judge" in html
        assert html.count('data-testid="failure-badge"') == 3
        assert "Target · rate limited" in html and "Judge · unavailable" in html
        assert 'data-role-filter="judge"' in html and 'data-failure-role="target"' in html

    def test_old_result_without_structure_renders_as_before(self):
        ScenarioResultFactory(
            run_id=self.run.id, status="failed",
            result={"severity": "ERROR", "summary": "old failure text", "error": "boom"},
        )
        html = self.client.get(f"/runs/{self.run.id}/results-fragment/").content.decode()
        assert "old failure text" in html
        assert "failure-badge" not in html and "role-filter" not in html and "Filter:" not in html

    def test_detail_shows_badge_and_raw_message(self):
        row = self._fail("auditor", message="APITimeoutError: Request timed out.", kind="timeout",
                         endpoint="http://auditor/v1")
        html = self.client.get(f"/runs/{self.run.id}/results/{row.pk}/").content.decode()
        assert 'data-testid="failure-detail"' in html
        assert "Auditor · timeout" in html and "http://auditor/v1" in html
        assert "APITimeoutError: Request timed out." in html

    def test_old_result_detail_has_no_badge(self):
        row = ScenarioResultFactory(
            run_id=self.run.id, status="failed", result={"severity": "ERROR", "error": "boom"},
        )
        html = self.client.get(f"/runs/{self.run.id}/results/{row.pk}/").content.decode()
        assert "Scenario failed: boom" in html and "failure-detail" not in html


def test_result_is_json_serialisable():
    payload = _error_result(ERRORS["429"][0], "judge")
    attach_failures(payload, PhaseTracker(), SNAPSHOTS)
    json.dumps(payload)
