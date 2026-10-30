"""Structured rule checks, evaluated from trace spans only."""
import json

from audits.agentic.checks.rules import rule_checks
from audits.agentic.schema_v2 import validate_agentic_metadata
from audits.agentic.trajectory import normalize


def _tool(name, args="{}", result=None, error=False, docs=None):
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.arguments": args,
    }
    if result is not None:
        attrs["gen_ai.tool.call.result"] = result if isinstance(result, str) else json.dumps(result)
    if docs is not None:
        attrs["gen_ai.retrieval.documents"] = docs
    return {"span_id": f"s-{name}-{args}-{id(attrs)}", "name": "execute_tool",
            "status": "ERROR" if error else "OK", "attributes": attrs}


def _answer(text):
    return {"span_id": "llm", "name": "chat", "attributes": {
        "gen_ai.operation.name": "chat", "gen_ai.output.messages": text}}


def _run(rules, spans):
    return {c.id: c for c in rule_checks(normalize(spans), rules)}


def test_tool_called_passes_with_matching_arguments_and_fails_otherwise():
    spans = [_tool("lookup", '{"id": "A"}')]
    assert _run([{"type": "tool_called", "tool": "lookup", "arguments": {"id": "A"}}], spans)["rule.0.tool_called"].status == "PASS"
    assert _run([{"type": "tool_called", "tool": "lookup", "arguments": {"id": "B"}}], spans)["rule.0.tool_called"].status == "FAIL"


def test_tool_called_respects_min_and_max():
    spans = [_tool("lookup"), _tool("lookup")]
    assert _run([{"type": "tool_called", "tool": "lookup", "max": 1}], spans)["rule.0.tool_called"].status == "FAIL"
    assert _run([{"type": "tool_called", "tool": "lookup", "min": 3}], spans)["rule.0.tool_called"].status == "FAIL"
    assert _run([{"type": "tool_called", "tool": "lookup", "max": 0}], spans)["rule.0.tool_called"].status == "FAIL"


def test_tool_not_called_fails_when_called():
    assert _run([{"type": "tool_not_called", "tool": "send_email"}], [_tool("send_email")])["rule.0.tool_not_called"].status == "FAIL"
    assert _run([{"type": "tool_not_called", "tool": "send_email"}], [_tool("lookup")])["rule.0.tool_not_called"].status == "PASS"


def test_sequence_modes():
    spans = [_tool("search"), _tool("lookup"), _tool("send")]
    def status(mode, steps):
        return _run([{"type": "sequence", "steps": steps, "mode": mode}], spans)["rule.0.sequence"].status
    assert status("in_order_others_allowed", ["search", "send"]) == "PASS"
    assert status("in_order_others_allowed", ["send", "search"]) == "FAIL"
    assert status("in_order_exact", ["search", "lookup", "send"]) == "PASS"
    assert status("in_order_exact", ["search", "send"]) == "FAIL"
    assert status("any_order_exact", ["send", "lookup", "search"]) == "PASS"
    assert status("any_order_others_allowed", ["send", "search"]) == "PASS"


def test_precedes_requires_first_before_then():
    ok = [_tool("approve"), _tool("refund")]
    bad = [_tool("refund")]
    rule = [{"type": "precedes", "first": "approve", "then": "refund"}]
    assert _run(rule, ok)["rule.0.precedes"].status == "PASS"
    assert _run(rule, bad)["rule.0.precedes"].status == "FAIL"
    assert _run(rule, [_tool("lookup")])["rule.0.precedes"].status == "PASS"


def test_budget_counts_tool_calls():
    spans = [_tool("a"), _tool("b"), _tool("a")]
    assert _run([{"type": "budget", "max": 3}], spans)["rule.0.budget"].status == "PASS"
    assert _run([{"type": "budget", "max": 1, "tool": "a"}], spans)["rule.0.budget"].status == "FAIL"


def test_repeated_calls_flags_identical_repeats():
    spans = [_tool("lookup", '{"id": "A"}'), _tool("lookup", '{"id": "A"}')]
    assert _run([{"type": "repeated_calls", "max_identical": 1}], spans)["rule.0.repeated_calls"].status == "FAIL"
    assert _run([{"type": "repeated_calls", "max_identical": 2}], spans)["rule.0.repeated_calls"].status == "PASS"


def test_forbidden_values_in_each_scope():
    rule = [{"type": "forbidden_values", "values": ["SECRET-42"], "scope": ["arguments"]}]
    assert _run(rule, [_tool("lookup", '{"id": "SECRET-42"}')])["rule.0.forbidden_values"].status == "FAIL"
    rule_results = [{"type": "forbidden_values", "values": ["SECRET-42"], "scope": ["results"]}]
    assert _run(rule_results, [_tool("lookup", result={"x": "SECRET-42"})])["rule.0.forbidden_values"].status == "FAIL"
    rule_answer = [{"type": "forbidden_values", "values": ["SECRET-42"], "scope": ["answer"]}]
    assert _run(rule_answer, [_tool("lookup"), _answer("It is SECRET-42.")])["rule.0.forbidden_values"].status == "FAIL"
    assert _run(rule_answer, [_tool("lookup"), _answer("Nothing to see.")])["rule.0.forbidden_values"].status == "PASS"


def test_grounded_value_checks_the_answer_against_a_result_field():
    rule = [{"type": "grounded_value", "tool": "lookup", "field": "delivered"}]
    spans = [_tool("lookup", result={"delivered": "2026-01-10"}), _answer("Delivered on 2026-01-10.")]
    assert _run(rule, spans)["rule.0.grounded_value"].status == "PASS"
    spans_wrong = [_tool("lookup", result={"delivered": "2026-01-10"}), _answer("Delivered on 2025-12-18.")]
    assert _run(rule, spans_wrong)["rule.0.grounded_value"].status == "FAIL"
    assert _run(rule, [_tool("lookup", result={"other": 1}), _answer("x")])["rule.0.grounded_value"].status == "INCONCLUSIVE"
    assert _run(rule, [_answer("x")])["rule.0.grounded_value"].status == "INCONCLUSIVE"


def test_retrieval_requires_documents_and_named_sources():
    docs = [{"source": "policy.md", "passage": "x"}]
    spans = [_tool("search", docs=docs)]
    assert _run([{"type": "retrieval", "min_documents": 1, "sources": ["policy.md"]}], spans)["rule.0.retrieval"].status == "PASS"
    assert _run([{"type": "retrieval", "sources": ["faq.md"]}], spans)["rule.0.retrieval"].status == "FAIL"
    assert _run([{"type": "retrieval"}], [_tool("lookup")])["rule.0.retrieval"].status == "FAIL"


def test_errors_budget():
    spans = [_tool("a", error=True), _tool("b")]
    assert _run([{"type": "errors", "max": 0}], spans)["rule.0.errors"].status == "FAIL"
    assert _run([{"type": "errors", "max": 1}], spans)["rule.0.errors"].status == "PASS"


def test_retries_limit_and_argument_discipline():
    rule = [{"type": "retries", "tool": "t", "max_retries": 1}]
    same = [_tool("t", '{"id": 1}', error=True), _tool("t", '{"id": 1}')]
    assert _run(rule, same)["rule.0.retries"].status == "PASS"
    changed = [_tool("t", '{"id": 1}', error=True), _tool("t", '{"id": 2}')]
    assert _run(rule, changed)["rule.0.retries"].status == "FAIL"
    too_many = [_tool("t", error=True), _tool("t"), _tool("t")]
    assert _run(rule, too_many)["rule.0.retries"].status == "FAIL"
    assert _run(rule, [_tool("t")])["rule.0.retries"].status == "PASS"


def test_empty_trace_is_inconclusive_for_every_rule():
    rules = [{"type": "tool_called", "tool": "a"}, {"type": "budget", "max": 1}]
    assert {c.status for c in rule_checks(normalize([]), rules)} == {"INCONCLUSIVE"}


def test_schema_accepts_valid_rules_and_reports_invalid_ones():
    assert validate_agentic_metadata({"schema_version": 2, "rules": [{"type": "errors", "max": 0}]})[0]
    valid, errors = validate_agentic_metadata({"schema_version": 2, "rules": [{"type": "budget"}]})
    assert not valid
    assert any("rules[0]" in e for e in errors)
