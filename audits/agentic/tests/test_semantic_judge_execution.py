"""Semantic judge execution and verdict wiring (deterministic fake LLM, no network)."""
import json

from audits.agentic.evaluate import evaluate
from audits.agentic.orchestrator import orchestrate_agentic_audit
from audits.agentic.schema import AgentTrajectory, TrajectoryStep
from audits.agentic.semantic_judge import build_evidence_packet


def _trajectory() -> AgentTrajectory:
    return AgentTrajectory([
        TrajectoryStep("s1", "agent", "acme-agent"),
        TrajectoryStep(
            "t1", "tool", "query_knowledge_files", tool_name="query_knowledge_files",
            arguments={"query": "refund policy"},
            result=[{"source": "kb-acme", "passage": "Refunds within 30 days."}],
        ),
        TrajectoryStep(
            "r1", "retrieval", "retrieval",
            attributes={"gen_ai.retrieval.documents": [{"source": "kb-acme", "passage": "Refunds within 30 days."}]},
        ),
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
    assert "Refunds within 30 days." in packet["tool_steps"][0]["result"]
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


def test_packet_keeps_every_retrieved_source_and_drops_noise():
    from audits.agentic.trajectory import normalize

    docs = [{"source": f"doc{i}.md", "passage": "x" * 1000, "score": None} for i in range(5)]
    spans = [{"span_id": "t1", "name": "execute_tool acme_search_knowledge", "attributes": {
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "acme_search_knowledge",
        "gen_ai.tool.call.arguments": '{"query": "refund"}', "gen_ai.tool.call.result": json.dumps(docs)}}]
    packet = build_evidence_packet(normalize(spans), task="", expectations=[], final_response="")
    assert packet["retrieval_source_ids"] == [f"doc{i}.md" for i in range(5)]
    result = packet["tool_steps"][0]["result"]
    assert "score" not in result
    assert len(result) <= 1700


def test_packet_reads_retrieval_documents_sent_as_json_strings():
    # Open WebUI's instrumentation exports each document as its own JSON string.
    trajectory = AgentTrajectory([TrajectoryStep("r1", "retrieval", "retrieval", attributes={
        "gen_ai.retrieval.documents": [
            json.dumps({"document_id": "doc-1", "file_id": "f-1", "chunk_id": "c-1", "rank": 1, "score": 0.9}),
            json.dumps({"source": "customer_faq.md", "content": "Returns in 30 days."}),
            "shipping.md",
        ]})])
    packet = build_evidence_packet(trajectory)
    assert packet["retrieval_source_ids"] == ["customer_faq.md", "f-1", "shipping.md"]


def test_packet_takes_every_turn_from_the_trace_and_keeps_long_answers():
    """Each turn's user message and agent reply come from its model-call span; a long
    final answer is not cut before its conclusion."""
    import json

    from audits.agentic.semantic_judge import (
        build_agentic_semantic_prompt,
        build_evidence_packet,
    )
    from audits.agentic.trajectory import normalize

    answer = "The 15-day electronics window applies. " * 30 + "So your January 2nd deadline stands."

    def message(role, text):
        return {"role": role, "parts": [{"type": "text", "content": text}]}

    def chat(span_id, turn, start, inputs, reply):
        return {"span_id": span_id, "parent_span_id": turn, "name": "chat", "start_time": start,
                "attributes": {"gen_ai.operation.name": "chat",
                               "gen_ai.input.messages": json.dumps(inputs),
                               "gen_ai.output.messages": json.dumps([message("assistant", reply)])}}

    first = message("user", "How long do I have to return my headphones?")
    second = message("user", "Is there a high-value exception?")
    spans = [
        {"span_id": "wf1", "name": "invoke_workflow", "start_time": 1.0,
         "attributes": {"gen_ai.operation.name": "invoke_workflow"}},
        chat("c1", "wf1", 1.1, [first], "15 days from delivery: until January 2nd."),
        {"span_id": "wf2", "name": "invoke_workflow", "start_time": 5.0,
         "attributes": {"gen_ai.operation.name": "invoke_workflow"}},
        chat("c2", "wf2", 5.1, [first, message("assistant", "15 days."), second], answer),
    ]
    packet = build_evidence_packet(normalize(spans), task="How long do I have?", final_response=answer)
    assert packet["final_response"] == answer                       # 1,200+ chars, not truncated
    turns = packet["conversation"]
    assert [(t["role"], t["span_id"]) for t in turns] == [
        ("user", "c1"), ("assistant", "c1"), ("user", "c2"), ("assistant", "c2")]
    assert turns[2]["content"] == "Is there a high-value exception?"
    assert turns[3]["content"].endswith("deadline stands.")
    assert "whole" in build_agentic_semantic_prompt(packet, ["task_completion"])


def test_retrieval_sources_read_the_standard_json_documents_attribute():
    """gen_ai.retrieval.documents is JSON text: the packet lists source names, not the whole JSON."""
    import json

    from audits.agentic.semantic_judge import build_evidence_packet
    from audits.agentic.trajectory import normalize

    docs = [{"source": "refunds.md", "file_id": "f1", "content": "Electronics: 15 days."},
            {"source": "matrix.md", "content": "Defective: 90 days."}]
    span = {"span_id": "r1", "name": "retrieval", "start_time": 1.0,
            "attributes": {"gen_ai.operation.name": "retrieval", "gen_ai.retrieval.documents": json.dumps(docs)}}
    packet = build_evidence_packet(normalize([span]))
    assert packet["retrieval_source_ids"] == ["f1", "matrix.md", "refunds.md"]
