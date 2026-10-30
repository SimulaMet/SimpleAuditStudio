"""One rule engine: custom checks (count, exists, none, all, distinct) over trace units."""
import json

from audits.agentic.rule_engine import evaluate_check, validate_check
from audits.agentic.trajectory import normalize


def _tool(name, args="{}", result=None, error=False, docs=None):
    attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
             "gen_ai.tool.call.arguments": args}
    if result is not None:
        attrs["gen_ai.tool.call.result"] = result if isinstance(result, str) else json.dumps(result)
    if docs is not None:
        attrs["gen_ai.retrieval.documents"] = docs
    return {"span_id": f"s-{name}-{args}-{id(attrs)}", "name": "execute_tool",
            "status": "ERROR" if error else "OK", "attributes": attrs}


def _answer(text):
    return {"span_id": "llm", "name": "chat", "attributes": {
        "gen_ai.operation.name": "chat", "gen_ai.output.messages": text}}


TRACES = {
    "one_call": [_tool("lookup", '{"id": "A"}', {"status": "ok"})],
    "two_calls": [_tool("lookup", '{"id": "A"}'), _tool("lookup", '{"id": "B"}')],
    "none": [_tool("other")],
    "error": [_tool("lookup", error=True), _tool("lookup")],
    "leak_in_args": [_tool("lookup", '{"id": "SECRET-42"}')],
    "leak_in_result": [_tool("lookup", {"x": "SECRET-42"})],
    "leak_in_answer": [_tool("lookup"), _answer("It is SECRET-42.")],
    "clean": [_tool("lookup", '{"id": "A"}', {"x": 1}), _answer("All good.")],
    "seq_ok": [_tool("search"), _tool("lookup", '{"id": "A"}')],
    "seq_extra": [_tool("search"), _tool("other"), _tool("lookup", '{"id": "A"}')],
    "seq_reversed": [_tool("lookup", '{"id": "A"}'), _tool("search")],
    "repeat_same": [_tool("lookup", '{"id": "A"}'), _tool("lookup", '{"id": "A"}')],
}

def test_custom_count_check_reads_as_expected():
    rule = {"type": "check", "select": {"kind": "tool", "where": [{"field": "tool.name", "op": "equals", "value": "lookup"}]},
            "check": "count", "expect": {"op": "at_least", "value": 2}}
    assert evaluate_check(0, rule, normalize(TRACES["two_calls"])).status == "PASS"
    assert evaluate_check(0, rule, normalize(TRACES["one_call"])).status == "FAIL"


def test_custom_all_check_fails_when_one_unit_breaks_the_condition():
    rule = {"type": "check", "select": {"kind": "tool", "where": [{"field": "tool.error", "op": "equals", "value": False}]},
            "check": "all"}
    assert evaluate_check(0, rule, normalize(TRACES["error"])).status == "FAIL"
    assert evaluate_check(0, rule, normalize(TRACES["two_calls"])).status == "PASS"


def test_custom_distinct_check_counts_values():
    rule = {"type": "check", "select": {"kind": "tool", "where": []}, "check": "distinct",
            "field": "tool.arguments", "expect": {"op": "at_most", "value": 1}}
    assert evaluate_check(0, rule, normalize(TRACES["two_calls"])).status == "FAIL"
    assert evaluate_check(0, rule, normalize(TRACES["one_call"])).status == "PASS"


def test_missing_answer_is_inconclusive_not_pass():
    rule = {"type": "check", "select": {"kind": "answer", "where": [{"field": "answer.text", "op": "contains", "value": "x"}]},
            "check": "none"}
    assert evaluate_check(0, rule, normalize(TRACES["one_call"])).status == "INCONCLUSIVE"


def test_unrecorded_field_is_inconclusive():
    rule = {"type": "check", "select": {"kind": "tool", "where": [{"field": "tool.result.status", "op": "equals", "value": "ok"}]},
            "check": "count", "expect": {"op": "at_least", "value": 1}}
    assert evaluate_check(0, rule, normalize([_tool("lookup")])).status == "INCONCLUSIVE"


def test_validation_explains_problems_in_plain_words():
    assert validate_check({"select": {"kind": "tool"}, "check": "count", "expect": {"op": "at_least", "value": 1}}) == []
    problems = validate_check({"select": {"kind": "tool", "where": [{"field": "tool.nope", "op": "equals", "value": 1}]},
                               "check": "count"})
    assert any("unknown field 'tool.nope'" in p for p in problems)
    assert any("'expect' needs an operator" in p for p in problems)
    assert any("operator 'contains' does not fit" in p for p in validate_check(
        {"select": {"kind": "tool", "where": [{"field": "span.error", "op": "contains", "value": "x"}]},
         "check": "none"}))


def test_no_tool_calls_is_a_real_zero_not_missing_evidence():
    rule = {"type": "check", "select": {"kind": "tool", "where": [{"field": "tool.name", "op": "equals", "value": "refund"}]},
            "check": "none"}
    trajectory = normalize([_answer("I cannot do that.")])
    assert evaluate_check(0, rule, trajectory).status == "PASS"


def test_error_type_on_a_trace_without_errors_is_a_real_no_match_not_missing_evidence():
    rule = {"type": "check", "select": {"kind": "any", "where": [
        {"field": "span.error", "op": "equals", "value": True},
        {"field": "span.error_type", "op": "equals", "value": "ValueError"}]}, "check": "exists"}
    result = evaluate_check(0, rule, normalize([_tool("lookup"), _answer("ok")]))
    assert result.status == "FAIL"


def test_all_check_with_nothing_to_check_is_inconclusive_not_pass():
    rule = {"type": "check", "select": {"kind": "tool", "where": [
        {"field": "tool.name", "op": "equals", "value": "lookup"}]}, "check": "all"}
    assert evaluate_check(0, rule, normalize([_answer("no tools")])).status == "INCONCLUSIVE"
