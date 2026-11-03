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


def _retrieval(documents):
    return {"span_id": "r1", "name": "retrieval", "attributes": {
        "gen_ai.operation.name": "retrieval", "gen_ai.retrieval.documents": documents}}


def test_retrieval_documents_sent_as_json_strings_are_read():
    # Open WebUI's instrumentation exports each document as its own JSON string, without a source.
    owui = normalize([_retrieval([
        json.dumps({"document_id": "doc-1", "file_id": "f-1", "chunk_id": "c-1", "rank": 1, "score": 0.9,
                    "content": "Returns in 30 days."}),
        "not json",
    ])])
    count = {"type": "check", "select": {"kind": "retrieval_document", "where": []}, "check": "count",
             "expect": {"op": "equals", "value": 1}}
    text = {"type": "check", "select": {"kind": "retrieval_document", "where": [
        {"field": "retrieval.text", "op": "contains", "value": "30 days"}]}, "check": "exists"}
    named = {"type": "check", "select": {"kind": "retrieval_document", "where": [
        {"field": "retrieval.source", "op": "equals", "value": "customer_faq.md"}]}, "check": "exists"}
    assert evaluate_check(0, count, owui).status == "PASS"
    assert evaluate_check(1, text, owui).status == "PASS"
    assert evaluate_check(2, named, owui).status == "INCONCLUSIVE"
    sourced = normalize([_retrieval([json.dumps({"source": "customer_faq.md", "content": "Returns in 30 days."})])])
    assert evaluate_check(2, named, sourced).status == "PASS"


def _tool_rule(op, value, field="tool.name", check="none"):
    return {"type": "check", "select": {"kind": "tool", "where": [{"field": field, "op": op, "value": value}]},
            "check": check}


def test_not_one_of_reads_the_comma_string_the_editor_stored_as_a_list():
    # The editor saved not_one_of values unsplit. Read char by char, "lookup,refund"
    # excluded nothing that is longer than one letter, so this check passed.
    for value in (["lookup", "refund"], "lookup,refund", "lookup, refund"):
        rule = _tool_rule("not_one_of", value, check="all")
        assert evaluate_check(0, rule, normalize(TRACES["one_call"])).status == "FAIL", value


def test_one_of_with_a_comma_string_matches_whole_values():
    rule = _tool_rule("one_of", "search,lookup", check="exists")
    assert evaluate_check(0, rule, normalize(TRACES["one_call"])).status == "PASS"
    assert evaluate_check(0, _tool_rule("one_of", "l,o", check="exists"), normalize(TRACES["one_call"])).status == "FAIL"


def test_between_condition_reads_two_numbers():
    trace = normalize([_tool("lookup", '{"n": 3}')])
    for value in ([1, 5], "1,5"):
        assert evaluate_check(0, _tool_rule("between", value, "tool.argument.n", "exists"), trace).status == "PASS"
    assert evaluate_check(0, _tool_rule("between", [4, 5], "tool.argument.n", "exists"), trace).status == "FAIL"


def test_list_operators_need_list_values():
    assert validate_check(_tool_rule("not_one_of", ["lookup"])) == []
    assert validate_check(_tool_rule("not_one_of", "lookup,refund")) == []
    assert validate_check(_tool_rule("one_of", 5)) == ["condition 1: 'one_of' needs one or more values"]
    assert validate_check(_tool_rule("not_one_of", "")) == ["condition 1: 'not_one_of' needs one or more values"]
    assert validate_check(_tool_rule("between", [1, 5], "tool.argument.n")) == []
    for bad in (None, [5, 1], "1", ["a", "b"]):
        assert validate_check(_tool_rule("between", bad, "tool.argument.n")) == [
            "condition 1: 'between' needs two numbers, low first"], bad


def test_at_least_and_at_most_conditions_compare_numbers():
    trace = normalize([_tool("lookup", '{"n": 3}')])
    assert evaluate_check(0, _tool_rule("at_least", 2, "tool.argument.n", "exists"), trace).status == "PASS"
    assert evaluate_check(0, _tool_rule("at_least", "2", "tool.argument.n", "exists"), trace).status == "PASS"
    assert evaluate_check(0, _tool_rule("at_most", 2, "tool.argument.n", "exists"), trace).status == "FAIL"


def test_numeric_conditions_need_numbers():
    assert validate_check(_tool_rule("at_least", 2, "tool.argument.n")) == []
    for op in ("at_least", "at_most"):
        for bad in (None, "abc", "", float("nan"), True):
            assert validate_check(_tool_rule(op, bad, "tool.argument.n")) == [f"condition 1: '{op}' needs a number"], bad


def test_a_bad_value_leaves_only_its_own_rule_inconclusive():
    # The old editor stored NaN as null; the engine raised and the whole scenario
    # ended as one agentic.evaluate ERROR. Now only that rule says what is wrong.
    trace = normalize([_tool("lookup", '{"n": 3}')])
    for op, value in (("at_least", None), ("at_most", "abc"), ("between", None), ("one_of", 5)):
        check = evaluate_check(0, _tool_rule(op, value, "tool.argument.n", "exists"), trace)
        assert check.status == "INCONCLUSIVE", (op, value)
        assert check.summary.startswith("Condition 1: "), check.summary
    count = {"type": "check", "select": {"kind": "tool", "where": []}, "check": "count", "expect": {"op": "at_least", "value": None}}
    assert evaluate_check(0, count, trace).status == "INCONCLUSIVE"


def test_audit_keeps_the_other_rules_when_one_value_is_bad():
    from audits.agentic.evaluate import evaluate
    from audits.agentic.expectations import validate_agentic_metadata

    good = _tool_rule("equals", "lookup", check="exists")
    meta = {"agentic": {"schema_version": 2, "rules": [good, _tool_rule("at_least", None, "tool.argument.n", "exists")]}}
    try:  # saving (the default) still rejects the value
        validate_agentic_metadata(meta)
        raise AssertionError("a save must reject the bad value")
    except ValueError as exc:
        assert "rule 2: condition 1: 'at_least' needs a number" in str(exc)
    validate_agentic_metadata(meta, values=False)  # an audit reports it on its rule instead
    out = evaluate(normalize([_tool("lookup", '{"n": 3}')]), meta)
    statuses = {c["id"]: c["status"] for c in out["checks"] if c["id"].startswith("rule.")}
    assert statuses == {"rule.0.check": "PASS", "rule.1.check": "INCONCLUSIVE"}
