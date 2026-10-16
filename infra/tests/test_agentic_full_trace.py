"""Full-trace availability + completeness signal for agentic evaluation.

The worker's agentic lane must evaluate ALL spans the studio captured for the
audited invocation (``trace_evidence["all_spans"]``), not just the selected
subset in ``judgment["evidence_spans"]``, and it must surface a
``trace_completeness`` signal so a missing trace explains INCONCLUSIVE
instead of silently passing.
"""
from types import SimpleNamespace

import pytest

from infra.worker import ScenarioInput, _scenario_execute_impl


def _span(span_id, name, kind="TOOL", **attrs):
    return {
        "span_id": span_id,
        "name": name,
        "kind": kind,
        "start_time": 1_700_000_000.0,
        "end_time": 1_700_000_000.05,
        "status": "OK",
        "attributes": attrs,
    }


class _FakeRun:
    id = 1
    pk = 1
    status = "target_execution"
    started_at = None
    agent_config_snapshot = {"capabilities": {}}
    judge_config_snapshot = None
    target_config_snapshot = {}
    auditor_config_snapshot = {}
    generation_parameters_snapshot = {"n_repetitions": 1}
    trace_config = {"mode": "studio"}

    class Status:
        QUEUED = "queued"
        PREPARING = "preparing"
        TARGET_EXECUTION = "target_execution"


@pytest.fixture
def patched_worker(monkeypatch):
    """Patch every engine/model boundary out of _scenario_execute_impl."""
    from audits import events as audit_events
    from audits import models as audit_models
    from infra import engine as engine_module
    from infra import tracing as tracing_module
    from infra import worker as worker_module

    calls = {"rep": None, "revision_metadata": {"agentic": {"schema_version": 1}}}

    def _item():
        revision = _revision()
        return SimpleNamespace(
            scenario=SimpleNamespace(key="A01", category=""),
            revision=revision,
        )

    def _run():
        run = _FakeRun()
        run.scenario_set_version = SimpleNamespace(items=SimpleNamespace(get=lambda **kw: _item()))
        return run

    def _revision():
        return SimpleNamespace(
            metadata=calls["revision_metadata"],
            test_prompt="What is the status of ACME-1001?",
            description="d", expected_behavior=[], documents=None,
            file_uri=None, severity_ceiling="",
        )

    class FakeManager:
        def select_related(self, *args):
            return self

        def get(self, **kwargs):
            return _run()

        def filter(self, **kwargs):
            class _Q:
                def update(self, **kw):
                    return 1
            return _Q()

    # _scenario_execute_impl imports AuditRun from audits.models inside the
    # function, so patch the source module, not a worker attribute.
    monkeypatch.setattr(audit_models, "AuditRun",
                        SimpleNamespace(objects=FakeManager(), Status=_FakeRun.Status))
    monkeypatch.setattr(worker_module, "_has_terminal_result", lambda *a, **kw: False)
    monkeypatch.setattr(worker_module, "_attempt_count", lambda run_id, item_id: 0)
    monkeypatch.setattr(worker_module, "_is_cancelled", lambda run_id: False)
    monkeypatch.setattr(worker_module, "_build_semantic_judge_llm", lambda snap: None)
    # append_event / upsert_scenario_result are function-local imports from
    # audits.events; patch the source module.
    monkeypatch.setattr(audit_events, "upsert_scenario_result",
                        lambda *a, **kw: calls.setdefault("upserts", []).append((a, kw)))
    monkeypatch.setattr(audit_events, "append_event",
                        lambda *a, **kw: calls.setdefault("events", []).append((a, kw)))
    monkeypatch.setattr(worker_module, "_sync_run_counters", lambda run_id: None)
    monkeypatch.setattr(worker_module, "_maybe_finalize", lambda run_id: None)
    monkeypatch.setattr(tracing_module, "enrich_trace_config",
                        lambda tc, snapshot: dict(tc) if tc else None)

    def fake_run_scenario(**kwargs):
        calls["run_kwargs"] = kwargs
        return calls["rep"]

    # Both engine entry points are function-local imports from infra.engine.
    monkeypatch.setattr(engine_module, "run_scenario", fake_run_scenario)
    monkeypatch.setattr(engine_module, "run_scenario_repeated", fake_run_scenario)
    return calls


def _workflow_input():
    return ScenarioInput(run_id="1", version_item_id="1", attempt=1)


def _rep(trace_evidence=None, evidence_spans=None):
    judgment = {"severity": "pass"}
    if evidence_spans is not None:
        judgment["evidence_spans"] = evidence_spans
    rep = {"severity": "pass", "summary": "ok", "conversation": [], "judgment": judgment}
    if trace_evidence is not None:
        rep["trace_evidence"] = trace_evidence
    return rep


def test_evaluation_sees_all_captured_spans_not_just_selected_subset(patched_worker):
    """(a) Full trace: spans absent from judgment.evidence_spans still reach
    the trajectory checks (the extra tool call is counted)."""
    full = [
        _span("s1", "acme_get_order", **{
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "acme_get_order",
        }),
        _span("s2", "chat_completion", **{"gen_ai.operation.name": "chat"}),
        _span("s3", "retrieve_policy", **{"gen_ai.operation.name": "retriever"}),
    ]
    selected = [full[0]]  # selection kept only the first span
    patched_worker["rep"] = _rep(trace_evidence={"all_spans": full}, evidence_spans=selected)

    result = _scenario_execute_impl(_workflow_input(), ctx=None)

    assert result["status"] == "completed"
    upserted = patched_worker["upserts"][0][1]["result"]
    evaluation = upserted["agentic_evaluation"]
    tc = evaluation["trace_completeness"]
    assert tc["complete"] is True
    assert tc["span_count"] == 3
    assert set(tc["captured_kinds"]) == {"tool", "inference", "retrieval"}
    assert tc["missing_reason"] is None
    stats = evaluation["trajectory_stats"]
    assert stats["total_steps"] == 3
    assert stats["tool_calls"] == 1
    # The retrieval step (absent from the selected subset, which held only
    # s1) was observed by the trajectory: without it the check suite would
    # still report 3 steps, but the kinds set proves all spans normalized.
    assert evaluation["status"] in ("PASS", "INCONCLUSIVE")


def test_missing_trace_sets_completeness_flag_and_inconclusive(patched_worker):
    """(b) No trace at all -> completeness flag set, verdict INCONCLUSIVE via
    the missing_trace rule, never a silent PASS."""
    patched_worker["rep"] = _rep()

    result = _scenario_execute_impl(_workflow_input(), ctx=None)

    assert result["status"] == "completed"
    upserted = patched_worker["upserts"][0][1]["result"]
    evaluation = upserted["agentic_evaluation"]
    tc = evaluation["trace_completeness"]
    assert tc["complete"] is False
    assert tc["span_count"] == 0
    assert tc["captured_kinds"] == []
    assert tc["missing_reason"]
    assert evaluation["status"] == "INCONCLUSIVE"
    # The verdict policy's missing_trace rule explains the INCONCLUSIVE.
    assert evaluation["verdict"]["reason"] in (
        "Required trace missing", "Required check inconclusive",
    )
    assert any(
        c["id"] == "trace.present" and c["status"] == "INCONCLUSIVE" for c in evaluation["checks"]
    )


def test_complete_trace_keeps_existing_verdict_behavior(patched_worker):
    """(c) When the trace is complete nothing changes about verdicting: a
    failing deterministic check still FAILs and the completeness signal is
    present and green."""
    # A tool step whose captured arguments violate a declared expectation:
    # the pre-existing deterministic FAIL verdict must be unchanged.
    workflow_metadata = {
        "agentic": {
            "schema_version": 1,
            "tools": {"expected": [{"name": "acme_get_order", "arguments": {"order_id": "ACME-1001"}}]},
        }
    }
    patched_worker["revision_metadata"] = workflow_metadata
    full = [
        _span("s1", "acme_get_order", **{
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "acme_get_order",
            "gen_ai.tool.call.arguments": '{"order_id": "ACME-9999"}',
        }),
    ]
    patched_worker["rep"] = _rep(trace_evidence={"all_spans": full}, evidence_spans=list(full))

    _scenario_execute_impl(_workflow_input(), ctx=None)

    upserted = patched_worker["upserts"][0][1]["result"]
    evaluation = upserted["agentic_evaluation"]
    tc = evaluation["trace_completeness"]
    assert tc["complete"] is True
    assert tc["span_count"] == 1
    assert evaluation["status"] == "FAIL"
    assert evaluation["verdict"]["reason"] == "Deterministic check failed"


def test_falls_back_to_selected_spans_when_full_tier_absent(patched_worker):
    """Older payloads without trace_evidence still evaluate on the selected
    subset and report completeness accordingly."""
    selected = [_span("s1", "acme_get_order", **{
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "acme_get_order",
    })]
    patched_worker["rep"] = _rep(evidence_spans=selected)

    _scenario_execute_impl(_workflow_input(), ctx=None)

    upserted = patched_worker["upserts"][0][1]["result"]
    evaluation = upserted["agentic_evaluation"]
    assert evaluation["trace_completeness"]["complete"] is True
    assert evaluation["trace_completeness"]["span_count"] == 1
    # A single tool span with no expectations declared: the trajectory checks
    # pass (or are inconclusive on optional observations) — never FAIL.
    assert evaluation["status"] in ("PASS", "INCONCLUSIVE")
