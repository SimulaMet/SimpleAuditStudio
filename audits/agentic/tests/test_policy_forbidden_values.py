"""Forbidden values are checked in tool calls, tool results and the final answer, for any tool."""
from audits.agentic.checks.policy import policy_checks
from audits.agentic.trajectory import normalize


def _tool(name, args, result):
    return {
        "span_id": f"t-{name}",
        "name": "execute_tool",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.arguments": args,
            "gen_ai.tool.call.result": result,
        },
    }


def _answer(text):
    return {
        "span_id": "llm",
        "name": "chat",
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.output.messages": text,
        },
    }


def _statuses(spans, values=("SECRET-42",)):
    checks = policy_checks(normalize(spans), {"forbidden_data_scopes": list(values)})
    return {c.id: c.status for c in checks}


def test_clean_run_passes_all_three():
    s = _statuses([_tool("lookup", '{"id": "A"}', '{"ok": true}'), _answer("All good.")])
    assert s == {
        "policy.forbidden_value_in_call": "PASS",
        "policy.forbidden_value_in_result": "PASS",
        "policy.forbidden_value_in_answer": "PASS",
    }


def test_forbidden_value_in_a_call_fails_the_call_check():
    s = _statuses([_tool("lookup", '{"id": "SECRET-42"}', '{"ok": false}')])
    assert s["policy.forbidden_value_in_call"] == "FAIL"


def test_forbidden_value_in_a_result_fails_the_result_check():
    s = _statuses([_tool("lookup", '{"id": "A"}', '{"secret": "SECRET-42"}')])
    assert s["policy.forbidden_value_in_result"] == "FAIL"


def test_forbidden_value_in_the_answer_fails_the_answer_check():
    s = _statuses([_tool("lookup", '{"id": "A"}', '{"ok": true}'), _answer("It is SECRET-42.")])
    assert s["policy.forbidden_value_in_answer"] == "FAIL"


def test_no_trace_is_inconclusive():
    s = _statuses([])
    assert set(s.values()) == {"INCONCLUSIVE"}
