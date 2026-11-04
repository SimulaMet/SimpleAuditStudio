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
