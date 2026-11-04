"""Studio stores GenAI span content once per trace, in Open WebUI's reference format."""
import json

from audits.agentic.compact import compact_spans, expand_spans

SYSTEM = {"role": "system", "parts": [{"type": "text", "content": "You are the Acme Retail Support Refund Assistant. " * 2}]}
QUESTION = {"role": "user", "parts": [{"type": "text", "content": "Which document sets the electronics return window?"}]}
CALL = {"role": "assistant", "parts": [{"type": "text", "content": ""}, {
    "type": "tool_call", "id": "c1", "name": "query_knowledge_files", "arguments": {"query": "electronics window"}}]}
CHUNKS = [{"content": "Electronics keep the 15-day refund window from the policy.", "source": "a.md"},
          {"content": "A defective electronic device is covered for up to 90 days.", "source": "b.md"}]
ANSWER = {"role": "assistant", "parts": [{"type": "text", "content": "Electronics have a 15-day window (a.md)."}]}


def _span(span_id, op, start, trace="t1", **attrs):
    return {"trace_id": trace, "span_id": span_id, "parent_span_id": "wf", "name": op,
            "start_time": start, "end_time": start + 0.1,
            "attributes": {"gen_ai.operation.name": op,
                           **{k: json.dumps(v) for k, v in attrs.items()}}}


def _turn(start=1.0, history=()):
    history = list(history)
    return [
        _span(f"l1-{start}", "chat", start, **{"gen_ai.input.messages": [SYSTEM, *history, QUESTION],
                                               "gen_ai.output.messages": [{**CALL, "finish_reason": "tool_call"}]}),
        _span(f"t1-{start}", "execute_tool", start + 1, **{"gen_ai.tool.call.id": "c1",
                                                            "gen_ai.tool.call.result": {"content": CHUNKS}}),
        _span(f"l2-{start}", "chat", start + 2, **{
            "gen_ai.input.messages": [SYSTEM, *history, QUESTION, CALL, {
                "role": "tool", "parts": [{"type": "tool_call_response", "id": "c1", "response": CHUNKS}]}],
            "gen_ai.output.messages": [ANSWER]}),
    ]


def _input(span):
    return json.loads(span["attributes"]["gen_ai.input.messages"])


def test_tool_loop_stores_each_piece_once():
    first, tool, second = compact_spans(_turn())
    assert len(_input(first)) == 2
    system, question, call, response = _input(second)
    assert system == {"role": "system", "parts": [
        {"type": "reference", "span_id": "l1-1.0", "attribute": "gen_ai.input.messages", "index": 0}]}
    assert question["parts"][0]["index"] == 1
    assert call["parts"] == [{"type": "reference", "span_id": "l1-1.0", "attribute": "gen_ai.output.messages", "index": 0}]
    assert response["parts"][0]["response"] == {"type": "reference", "span_id": "t1-1.0", "attribute": "gen_ai.tool.call.result"}
    assert json.loads(tool["attributes"]["gen_ai.tool.call.result"]) == {"content": CHUNKS}  # first occurrence kept


def test_round_trip_restores_full_content():
    original = _turn()
    expanded = expand_spans(compact_spans(original))
    restored = _input(expanded[2])
    assert restored[:2] == [SYSTEM, QUESTION]
    assert restored[2]["parts"][-1]["name"] == "query_knowledge_files"
    assert restored[3]["parts"][0]["response"] == {"content": CHUNKS}


def test_multi_turn_history_refers_to_earlier_turns():
    turn1 = _turn(1.0)
    turn2 = _turn(10.0, history=[QUESTION, ANSWER])
    spans = compact_spans(turn1 + turn2)
    messages = _input(spans[3])            # turn 2, first model call
    assert all(m["parts"][0]["type"] == "reference" for m in messages)
    assert messages[2]["parts"][0] == {"type": "reference", "span_id": "l2-1.0",
                                       "attribute": "gen_ai.output.messages", "index": 0}


def test_compaction_is_idempotent_and_keeps_open_webui_references():
    once = compact_spans(_turn())
    assert compact_spans(once) == once


def test_other_traces_are_not_referenced():
    a = _turn(1.0)
    b = [dict(s, trace_id="t2", span_id=s["span_id"] + "-b") for s in _turn(5.0)]
    spans = compact_spans(a + b)
    assert len([m for m in _input(spans[3]) if m["parts"][0]["type"] == "reference"]) == 0


def test_evidence_subset_expands_from_the_full_capture():
    compacted = compact_spans(_turn())
    (second,) = expand_spans([compacted[2]], pool=compacted)
    assert _input(second)[0] == SYSTEM


def test_unresolvable_reference_is_left_in_place():
    compacted = compact_spans(_turn())
    (second,) = expand_spans([compacted[2]])          # no pool: the first call is missing
    assert _input(second)[0]["parts"][0]["type"] == "reference"


def test_workflow_wrapper_is_never_the_referenced_copy():
    wrapper = _span("wf", "invoke_workflow", 0.5, **{"gen_ai.input.messages": [QUESTION]})
    spans = compact_spans([wrapper, *_turn()])
    assert json.loads(spans[0]["attributes"]["gen_ai.input.messages"]) == [QUESTION]
    assert _input(spans[1])[1] == QUESTION          # LLM-1 keeps the user message in full


def test_knowledge_tool_result_refers_to_the_retrieval_documents():
    """Retrieved chunks are kept once, as the retrieval documents, though the tool span starts first."""
    docs = [{"source": "a.md", "content": "Electronics keep the 15-day refund window from the policy."}]
    tool = _span("tool", "execute_tool", 1.0, **{"gen_ai.tool.call.result": {"content": docs}})
    retrieval = _span("ret", "retrieval", 1.1, **{"gen_ai.retrieval.documents": docs})
    tool_out, retrieval_out = compact_spans([tool, retrieval])
    assert json.loads(tool_out["attributes"]["gen_ai.tool.call.result"]) == {
        "type": "reference", "span_id": "ret", "attribute": "gen_ai.retrieval.documents"}
    assert json.loads(retrieval_out["attributes"]["gen_ai.retrieval.documents"]) == docs


def test_judge_view_keeps_gen_ai_content_once():
    from audits.agentic.compact import judge_view

    wrapper = _span("wf", "invoke_workflow", 0.5, **{"gen_ai.input.messages": [QUESTION]})
    spans = [wrapper, *_turn()]
    for s in spans:
        s["attributes"].update({"telemetry.sdk.name": "opentelemetry", "service.name": "open-webui"})
    answer_text = ANSWER["parts"][0]["content"]
    view = judge_view(spans, pool=spans, conversation=[{"role": "assistant", "content": answer_text}])
    assert [s["span_id"] for s in view] == ["l1-1.0", "t1-1.0", "l2-1.0"]          # no wrapper
    assert all(k.startswith("gen_ai.") for s in view for k in s["attributes"])
    second = view[2]["attributes"]
    assert list(second)[:2] == ["gen_ai.operation.name", "gen_ai.output.messages"]  # decisions first
    messages = json.loads(second["gen_ai.input.messages"])
    assert messages[0] == "(same as span l1-1.0 input.messages message 1, system)"
    out = json.loads(second["gen_ai.output.messages"])
    assert out[0]["parts"][0]["content"] == "(the assistant turn shown in the conversation)"


def test_judge_view_keeps_the_exception_message():
    from audits.agentic.compact import judge_view

    span = _span("t9", "execute_tool", 1.0, **{"gen_ai.tool.name": "acme_get_order"})
    span["attributes"].update({"error.type": "ValueError", "exception.type": "ValueError",
                               "exception.message": "No order found for 'ACME-9999'.", "service.name": "x"})
    (view,) = judge_view([span])
    assert view["attributes"]["exception.message"] == "No order found for 'ACME-9999'."
    assert "service.name" not in view["attributes"]
