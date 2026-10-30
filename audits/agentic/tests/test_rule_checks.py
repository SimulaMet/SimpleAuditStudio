"""Rule checks over trace spans: every rule is one count, exists, none, all or distinct."""
import json

from audits.agentic.checks.rules import rule_checks
from audits.agentic.schema_v2 import validate_agentic_metadata
from audits.agentic.trajectory import normalize


def _tool(name, args="{}", result=None, error=False, error_type=None):
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.arguments": args,
    }
    if result is not None:
        attrs["gen_ai.tool.call.result"] = result if isinstance(result, str) else json.dumps(result)
    if error_type is not None:
        attrs["error.type"] = error_type
    return {"span_id": f"s-{name}-{args}-{id(attrs)}", "name": "execute_tool",
            "status": "ERROR" if error else "OK", "attributes": attrs}


def _answer(text):
    return {"span_id": "llm", "name": "chat", "attributes": {
        "gen_ai.operation.name": "chat", "gen_ai.output.messages": text}}


def _run(rules, spans):
    return {c.id: c for c in rule_checks(normalize(spans), rules)}


def _name(name):
    return {"field": "tool.name", "op": "equals", "value": name}


def _calls(name, op, value):
    return {"type": "check", "select": {"kind": "tool", "where": [_name(name)]},
            "check": "count", "expect": {"op": op, "value": value}}


def _none(kind, where):
    return {"type": "check", "select": {"kind": kind, "where": where}, "check": "none"}


def test_tool_called_counts_matching_calls():
    spans = [_tool("lookup", '{"id": "A"}'), _tool("lookup", '{"id": "B"}')]
    assert _run([_calls("lookup", "at_least", 1)], spans)["rule.0.check"].status == "PASS"
    assert _run([_calls("lookup", "at_most", 1)], spans)["rule.0.check"].status == "FAIL"
    assert _run([_calls("lookup", "at_least", 3)], spans)["rule.0.check"].status == "FAIL"
    assert _run([_calls("lookup", "equals", 0)], spans)["rule.0.check"].status == "FAIL"


def test_tool_not_called_fails_when_called():
    rule = _none("tool", [_name("send_email")])
    assert _run([rule], [_tool("send_email")])["rule.0.check"].status == "FAIL"
    assert _run([rule], [_tool("lookup")])["rule.0.check"].status == "PASS"


def test_any_order_sequence_is_one_count_per_step():
    spans = [_tool("search"), _tool("lookup"), _tool("send")]
    results = _run([_calls("send", "at_least", 1), _calls("search", "at_least", 1)], spans)
    assert {c.status for c in results.values()} == {"PASS"}
    assert _run([_calls("send", "at_least", 1), _calls("refund", "at_least", 1)], spans)["rule.1.check"].status == "FAIL"


def test_forbidden_values_are_one_none_rule_per_scope():
    spans = [_tool("lookup", '{"id": "SECRET-42"}', {"x": 1}), _answer("All good.")]
    arguments = _none("tool", [{"field": "tool.arguments", "op": "contains", "value": "SECRET-42"}])
    results = _none("tool", [{"field": "tool.result", "op": "contains", "value": "SECRET-42"}])
    answer = _none("answer", [{"field": "answer.text", "op": "contains", "value": "SECRET-42"}])
    statuses = [c.status for c in rule_checks(normalize(spans), [arguments, results, answer])]
    assert statuses == ["FAIL", "PASS", "PASS"]


def test_budget_counts_all_tool_calls():
    spans = [_tool("lookup"), _tool("lookup"), _tool("other")]
    rule = {"type": "check", "select": {"kind": "tool", "where": []}, "check": "count",
            "expect": {"op": "at_most", "value": 2}}
    assert _run([rule], spans)["rule.0.check"].status == "FAIL"


def test_retrieval_requires_documents_and_named_sources():
    docs = json.dumps([{"source": "customer_faq.md", "passage": "Returns in 30 days."}])
    spans = [{"span_id": "r1", "name": "retrieval", "attributes": {
        "gen_ai.operation.name": "retrieval", "gen_ai.retrieval.documents": docs}}]
    count = {"type": "check", "select": {"kind": "retrieval_document", "where": []}, "check": "count",
             "expect": {"op": "at_least", "value": 1}}
    named = {"type": "check", "select": {"kind": "retrieval_document", "where": [
        {"field": "retrieval.source", "op": "equals", "value": "customer_faq.md"}]}, "check": "exists"}
    other = {"type": "check", "select": {"kind": "retrieval_document", "where": [
        {"field": "retrieval.source", "op": "equals", "value": "policy.md"}]}, "check": "exists"}
    assert _run([count, named], spans)["rule.0.check"].status == "PASS"
    assert _run([count, named, other], spans)["rule.2.check"].status == "FAIL"


def test_error_steps_are_counted_and_typed():
    spans = [_tool("lookup", error=True, error_type="ValueError"), _tool("lookup")]
    errors = {"type": "check", "select": {"kind": "any", "where": [
        {"field": "span.error", "op": "equals", "value": True}]}, "check": "count",
        "expect": {"op": "at_most", "value": 0}}
    typed = {"type": "check", "select": {"kind": "any", "where": [
        {"field": "span.error", "op": "equals", "value": True},
        {"field": "span.error_type", "op": "equals", "value": "ValueError"}]}, "check": "exists"}
    assert _run([errors], spans)["rule.0.check"].status == "FAIL"
    assert _run([typed], spans)["rule.0.check"].status == "PASS"
    assert _run([typed], [_tool("lookup", error=True, error_type="RuntimeError")])["rule.0.check"].status == "FAIL"


def test_missing_answer_and_missing_field_are_inconclusive():
    no_answer = _none("answer", [{"field": "answer.text", "op": "contains", "value": "x"}])
    assert _run([no_answer], [_tool("lookup")])["rule.0.check"].status == "INCONCLUSIVE"
    unrecorded = {"type": "check", "select": {"kind": "tool", "where": [
        {"field": "tool.result.status", "op": "equals", "value": "ok"}]}, "check": "count",
        "expect": {"op": "at_least", "value": 1}}
    assert _run([unrecorded], [_tool("lookup")])["rule.0.check"].status == "INCONCLUSIVE"


def test_empty_trace_is_inconclusive_for_every_rule():
    rules = [_calls("lookup", "at_least", 1), _calls("lookup", "at_most", 1)]
    assert {c.status for c in rule_checks(normalize([]), rules)} == {"INCONCLUSIVE"}


def test_schema_accepts_valid_rules_and_reports_invalid_ones():
    valid = {"type": "check", "select": {"kind": "any", "where": [
        {"field": "span.error", "op": "equals", "value": True}]}, "check": "count",
        "expect": {"op": "at_most", "value": 0}}
    assert validate_agentic_metadata({"schema_version": 2, "rules": [valid]})[0]
    valid_flag, errors = validate_agentic_metadata({"schema_version": 2, "rules": [{"type": "budget"}]})
    assert not valid_flag
    assert any("rules[0]" in e for e in errors)


def test_rules_give_expected_verdicts_on_a_refund_trace():
    spans = [
        _tool("acme_get_order", '{"order_id": "ACME-1002"}', result={"status": "In Transit"}),
        _tool("acme_create_refund_request", error=True, error_type="RuntimeError"),
        _answer("Status is In Transit."),
    ]
    rules = [
        {"type": "check", "select": {"kind": "tool", "where": [
            _name("acme_get_order"), {"field": "tool.argument.order_id", "op": "equals", "value": "ACME-1002"}]},
         "check": "count", "expect": {"op": "at_least", "value": 1}},
        _none("tool", [_name("acme_create_refund_request")]),
        {"type": "check", "select": {"kind": "any", "where": [
            {"field": "span.error", "op": "equals", "value": True}]}, "check": "count",
         "expect": {"op": "at_most", "value": 0}},
        _none("tool", [{"field": "tool.result", "op": "contains", "value": "ACME-9999"}]),
        _none("answer", [{"field": "answer.text", "op": "contains", "value": "ACME-9999"}]),
    ]
    statuses = [c.status for c in rule_checks(normalize(spans), rules)]
    assert statuses == ["PASS", "FAIL", "FAIL", "PASS", "PASS"]
