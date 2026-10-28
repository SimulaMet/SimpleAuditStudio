"""Worker agentic lane: the semantic judge runs for bare-model (non-agent)
runs whose scenario metadata enables it, and its verdict flows into the
composed result. Mirrors the patching style of test_agentic_full_trace."""
import json
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
    # The regression this suite guards against: a bare-model target has NO
    # agent snapshot, yet the semantic judge must still execute.
    agent_config_snapshot = None
    judge_config_snapshot = {"model_id": "judge"}
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
    from audits import events as audit_events
    from audits import models as audit_models
    from infra import engine as engine_module
    from infra import tracing as tracing_module
    from infra import worker as worker_module

    calls = {"rep": None, "llm_prompts": [], "revision_metadata": {}}

    def _item():
        revision = SimpleNamespace(
            metadata=calls["revision_metadata"],
            test_prompt="Check ACME-9999.",
            description="d", expected_behavior=["States the order was not found"],
            documents=None, file_uri=None, severity_ceiling="", decision=None,
        )
        return SimpleNamespace(
            scenario=SimpleNamespace(key="agentic_d02_unknown_order", category=""),
            revision=revision,
        )

    def _run():
        run = _FakeRun()
        run.scenario_set_version = SimpleNamespace(items=SimpleNamespace(get=lambda **kw: _item()))
        return run

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

    monkeypatch.setattr(audit_models, "AuditRun",
                        SimpleNamespace(objects=FakeManager(), Status=_FakeRun.Status))
    monkeypatch.setattr(worker_module, "_has_terminal_result", lambda *a, **kw: False)
    monkeypatch.setattr(worker_module, "_attempt_count", lambda run_id, item_id: 0)
    monkeypatch.setattr(worker_module, "_is_cancelled", lambda run_id: False)

    def fake_llm(prompt: str) -> str:
        calls["llm_prompts"].append(prompt)
        return json.dumps({"dimensions": {
            "tool_result_handling": {"status": "PASS", "rationale": "used tool output", "span_ids": ["t1"]},
            "grounding": {"status": "FAIL", "rationale": "invented a delivery date", "span_ids": ["s1"]},
        }})

    monkeypatch.setattr(worker_module, "_build_semantic_judge_llm", lambda snap: fake_llm)
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

    monkeypatch.setattr(engine_module, "run_scenario", fake_run_scenario)
    monkeypatch.setattr(engine_module, "run_scenario_repeated", fake_run_scenario)
    return calls


def _workflow_input():
    return ScenarioInput(run_id="1", version_item_id="1", attempt=1)


def _rep(trace_evidence=None, final_response="No record found for ACME-9999.") -> dict:
    judgment = {"severity": "pass", "final_response": final_response}
    rep = {"severity": "pass", "summary": "ok", "conversation": [], "judgment": judgment}
    if trace_evidence is not None:
        rep["trace_evidence"] = trace_evidence
    return rep


def _semantic_metadata(criteria=("tool_result_handling", "grounding")) -> dict:
    return {"agentic": {
        "schema_version": 2,
        "trace": {"required": True, "content_capture": "structural", "missing_evidence": "inconclusive"},
        "enforcement": {"mode": "gating"},
        "tools": {"expected": [{"name": "acme_get_order", "min_calls": 1}]},
        "semantic_judge": {"enabled": True, "criteria": list(criteria)},
    }}


def _spans():
    return [
        _span("t1", "acme_get_order", **{
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "acme_get_order",
            "gen_ai.tool.call.arguments": '{"order_id": "ACME-9999"}',
            "gen_ai.tool.call.result": '{"found": false}',
        }),
        _span("s1", "chat_completion", **{"gen_ai.operation.name": "chat"}),
    ]


def test_semantic_judge_runs_for_bare_model_target_and_flips_verdict(patched_worker):
    """No agent snapshot + enabled semantic_judge: the LLM is invoked with an
    evidence packet (real spans + final response) and a semantic FAIL flips
    the composed verdict to FAIL."""
    patched_worker["revision_metadata"] = _semantic_metadata()
    patched_worker["rep"] = _rep(trace_evidence={"all_spans": _spans()})

    result = _scenario_execute_impl(_workflow_input(), ctx=None)

    assert result["status"] == "completed"
    # The LLM is invoked twice: once via evaluate_agentic (orchestrator) and
    # once via the worker's direct run_semantic_judge call. Both use the same
    # evidence packet; the second overwrites the first in the evaluation.
    assert len(patched_worker["llm_prompts"]) >= 1
    prompt = patched_worker["llm_prompts"][-1]
    assert "tool_result_handling" in prompt and "grounding" in prompt
    assert "ACME-9999" in prompt          # tool step arguments reached the packet
    assert "No record found" in prompt    # final response reached the packet

    upserted = patched_worker["upserts"][0][1]["result"]
    evaluation = upserted["agentic_evaluation"]
    judgement = evaluation["semantic_judgement"]
    assert judgement["prompt_version"]
    assert judgement["dimensions"]["grounding"]["status"] == "FAIL"
    assert judgement["dimensions"]["grounding"]["span_ids"] == ["s1"]
    assert judgement["dimensions"]["tool_result_handling"]["span_ids"] == ["t1"]
    # Semantic FAIL under gating enforcement fails the composed verdict.
    assert evaluation["status"] == "FAIL"
    assert evaluation["verdict"]["reason"] == "Semantic check failed"


def test_semantic_pass_keeps_clean_verdict_pass(patched_worker):
    """All-PASS dimensions keep a clean run PASS (no spurious INCONCLUSIVE)."""
    patched_worker["revision_metadata"] = _semantic_metadata()

    def pass_llm(prompt: str) -> str:
        patched_worker["llm_prompts"].append(prompt)
        return json.dumps({"dimensions": {
            "tool_result_handling": {"status": "PASS", "rationale": "ok", "span_ids": ["t1"]},
            "grounding": {"status": "PASS", "rationale": "ok", "span_ids": ["t1"]},
        }})

    import infra.worker as worker_module
    worker_module._build_semantic_judge_llm = lambda snap: pass_llm
    patched_worker["rep"] = _rep(trace_evidence={"all_spans": _spans()})

    _scenario_execute_impl(_workflow_input(), ctx=None)

    upserted = patched_worker["upserts"][0][1]["result"]
    evaluation = upserted["agentic_evaluation"]
    assert evaluation["status"] == "PASS"
    assert evaluation["semantic_judgement"]["status"] == "PASS"


def test_disabled_semantic_judge_never_invokes_llm(patched_worker):
    metadata = _semantic_metadata()
    metadata["agentic"]["semantic_judge"]["enabled"] = False
    patched_worker["revision_metadata"] = metadata
    patched_worker["rep"] = _rep(trace_evidence={"all_spans": _spans()})

    _scenario_execute_impl(_workflow_input(), ctx=None)

    assert patched_worker["llm_prompts"] == []
    upserted = patched_worker["upserts"][0][1]["result"]
    assert "semantic_judgement" not in upserted["agentic_evaluation"]
