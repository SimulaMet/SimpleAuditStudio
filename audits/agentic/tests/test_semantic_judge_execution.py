"""Semantic judge execution and verdict wiring (deterministic fake LLM, no network)."""
import json

from audits.agentic.evaluate import evaluate
from audits.agentic.orchestrator import orchestrate_agentic_audit
from audits.agentic.schema import AgentTrajectory, TrajectoryStep


def _trajectory() -> AgentTrajectory:
    return AgentTrajectory([
        TrajectoryStep("s1", "agent", "acme-agent"),
        TrajectoryStep(
            "t1", "tool", "query_knowledge_files", tool_name="query_knowledge_files",
            arguments={"query": "refund policy"}, result="Refunds within 30 days.",
            source_refs=["kb-acme"],
        ),
        TrajectoryStep("r1", "retrieval", "retrieval", source_refs=["kb-acme"]),
    ])


def _metadata(criteria=("grounding", "task_completion")) -> dict:
    return {"agentic": {
        "schema_version": 2,
        "enforcement": {"mode": "gating"},
        "semantic_judge": {"enabled": True, "criteria": list(criteria)},
    }}


def test_judge_invoked_when_enabled_and_flips_verdict_to_fail():
    """(i) judge is invoked when enabled; (ii) semantic FAIL flips verdict to FAIL."""
    calls = []

    def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"dimensions": {
            "grounding": {"status": "FAIL", "rationale": "response contradicts kb-acme",
                           "span_ids": ["t1"]},
            "task_completion": {"status": "PASS", "rationale": "ok", "span_ids": ["s1"]},
        }})

    result = orchestrate_agentic_audit(
        {}, _trajectory(), _metadata(), llm=fake_llm, task="Check refund policy",
    )
    assert len(calls) == 1
    assert "grounding" in calls[0] and "t1" in calls[0]
    assert result["status"] == "FAIL"
    judgement = result["semantic_judgement"]
    assert judgement["status"] == "FAIL"
    assert judgement["dimensions"]["grounding"]["status"] == "FAIL"
    assert judgement["dimensions"]["grounding"]["span_ids"] == ["t1"]
    # model + prompt version strings recorded in the output payload
    assert judgement["prompt_version"]


def test_llm_unavailable_yields_uncertain_not_pass():
    """(iii) without an LLM endpoint, dimensions are UNCERTAIN — never PASS."""
    result = orchestrate_agentic_audit({}, _trajectory(), _metadata())
    assert result["status"] == "INCONCLUSIVE"
    judgement = result["semantic_judgement"]
    assert judgement["status"] == "UNCERTAIN"
    for dimension in judgement["dimensions"].values():
        assert dimension["status"] == "UNCERTAIN"
        assert "llm_unavailable" in dimension["rationale"]


def test_disabled_judge_not_invoked():
    """(iv) with semantic_judge disabled, the LLM is never called."""
    calls = []
    metadata = _metadata()
    metadata["agentic"]["semantic_judge"]["enabled"] = False
    result = orchestrate_agentic_audit(
        {}, _trajectory(), metadata, llm=lambda p: calls.append(p) or "{}",
    )
    assert calls == []
    # A disabled judge produces no observation: the key is absent (not a
    # null value) so consumers can tell "not configured" from UNCERTAIN.
    assert "semantic_judgement" not in result
    assert result["status"] == "PASS"


def test_advisory_mode_does_not_flip_verdict():
    """Semantic FAIL under advisory enforcement does not fail the run."""
    metadata = _metadata()
    metadata["agentic"]["enforcement"]["mode"] = "advisory"

    def fake_llm(prompt: str) -> str:
        return json.dumps({"dimensions": {
            "grounding": {"status": "FAIL", "rationale": "x", "span_ids": ["t1"]},
            "task_completion": {"status": "PASS", "rationale": "ok", "span_ids": ["s1"]},
        }})

    result = orchestrate_agentic_audit({}, _trajectory(), metadata, llm=fake_llm)
    assert result["status"] == "PASS"
    assert result["semantic_judgement"]["status"] == "FAIL"


def test_evidence_packet_contains_observed_facts_only():
    from audits.agentic.semantic_judge import build_evidence_packet

    packet = build_evidence_packet(_trajectory(), task="Do it", expectations=["use kb"])
    assert packet["task"] == "Do it"
    assert packet["tool_steps"][0]["name"] == "query_knowledge_files"
    assert "Refunds within 30 days." in packet["tool_steps"][0]["result_excerpt"]
    assert packet["retrieval_source_ids"] == ["kb-acme"]
    assert set(packet["span_ids"]) == {"s1", "t1", "r1"}


def test_legacy_evaluate_interface_wires_llm():
    calls = []

    def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"dimensions": {
            "grounding": {"status": "PASS", "rationale": "ok", "span_ids": ["t1"]},
            "task_completion": {"status": "PASS", "rationale": "ok", "span_ids": ["s1"]},
        }})

    result = evaluate(_trajectory(), _metadata(), llm=fake_llm, task="Check")
    assert len(calls) == 1
    assert result["status"] == "PASS"
    assert result["semantic_judgement"]["status"] == "PASS"
