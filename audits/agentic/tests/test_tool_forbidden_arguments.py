"""Forbidden tool entries: a name, or a name with argument values that make the call forbidden."""
from audits.agentic.checks.tools import tool_selection
from audits.agentic.trajectory import normalize


def _call(name, args):
    return {
        "span_id": f"c-{name}-{args}",
        "name": "execute_tool",
        "attributes": {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.arguments": args,
        },
    }


def _forbidden_status(spans, forbidden):
    checks = tool_selection(normalize(spans), {"forbidden": forbidden})
    return next(c for c in checks if c.id == "tool.forbidden").status


def test_plain_name_forbids_every_call_of_that_tool():
    assert _forbidden_status([_call("send_email", '{"to": "a"}')], ["send_email"]) == "FAIL"


def test_name_with_arguments_forbids_only_matching_calls():
    entry = {"name": "lookup", "arguments": {"id": "SECRET-42"}}
    assert _forbidden_status([_call("lookup", '{"id": "SECRET-42"}')], [entry]) == "FAIL"
    assert _forbidden_status([_call("lookup", '{"id": "OTHER"}')], [entry]) == "PASS"


def test_no_matching_calls_passes():
    assert _forbidden_status([_call("lookup", '{"id": "OTHER"}')], ["send_email"]) == "PASS"
