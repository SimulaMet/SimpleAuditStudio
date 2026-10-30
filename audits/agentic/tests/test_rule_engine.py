"""One rule engine: custom checks and presets compiled into it.

The compiled presets must give the same verdict as the original evaluators,
on the same traces. Retrieval counts documents now, so it is tested separately.
"""
import json

import pytest

from audits.agentic.checks.rules import _EVALUATORS
from audits.agentic.rule_engine import evaluate_check, evaluate_preset, validate_check
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
}

PRESETS = [
    {"type": "tool_called", "tool": "lookup", "min": 1},
    {"type": "tool_called", "tool": "lookup", "max": 1},
    {"type": "tool_called", "tool": "lookup", "arguments": {"id": "A"}, "min": 1},
    {"type": "tool_not_called", "tool": "lookup"},
    {"type": "tool_not_called", "tool": "lookup", "arguments": {"id": "A"}},
    {"type": "budget", "max": 1},
    {"type": "budget", "max": 2, "tool": "lookup"},
    {"type": "errors", "max": 0},
    {"type": "errors", "max": 1},
    {"type": "forbidden_values", "values": ["SECRET-42"]},
]


@pytest.mark.parametrize("rule", PRESETS, ids=lambda r: f"{r['type']}-{sorted(r.items())[1:]}"[:80])
@pytest.mark.parametrize("trace", list(TRACES))
def test_compiled_preset_matches_its_original_verdict(rule, trace):
    trajectory = normalize(TRACES[trace])
    original = _EVALUATORS[rule["type"]](0, rule, trajectory)
    compiled = evaluate_preset(0, rule, trajectory)
    # Deliberate change: a forbidden-value check with no final answer is
    # INCONCLUSIVE (missing evidence), not PASS.
    no_answer = not any(s.kind == "inference" for s in trajectory.primary_steps)
    if rule["type"] == "forbidden_values" and no_answer:
        expected = "FAIL" if original.status == "FAIL" else "INCONCLUSIVE"
        assert compiled.status == expected, (trace, original.summary, compiled.summary)
        return
    assert compiled.status == original.status, (rule, trace, original.summary, compiled.summary)


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
