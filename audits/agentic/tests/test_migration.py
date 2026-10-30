"""Legacy structured controls convert to rules only where the meaning is the same."""
from audits.agentic.migration import editor_rules, has_legacy, split_legacy
from audits.agentic.rules import validate_rules


def test_expected_tool_becomes_tool_called_with_bounds():
    rules, rest = split_legacy({"tools": {"expected": [
        {"name": "lookup", "min_calls": 1, "max_calls": 2, "arguments": {"id": 1}, "arguments_mode": "any"}]}})
    assert rules == [{"type": "tool_called", "tool": "lookup", "arguments": {"id": 1}, "min": 1, "max": 2}]
    assert rest == {}
    assert validate_rules(rules) == []


def test_forbidden_tools_become_tool_not_called():
    rules, _ = split_legacy({"tools": {"forbidden": ["send_email", {"name": "refund", "arguments": {"id": "x"}}]}})
    assert rules == [
        {"type": "tool_not_called", "tool": "send_email"},
        {"type": "tool_not_called", "tool": "refund", "arguments": {"id": "x"}},
    ]


def test_budgets_and_sequence_and_retrieval_convert():
    agentic = {
        "budgets": {"max_tool_calls": 3, "max_errors": 0},
        "trajectory": {"required_sequence": ["retrieval", "lookup"], "order_mode": "exact"},
        "retrieval": {"required": True, "sources": ["policy.md"], "min_documents": 1},
    }
    rules, rest = split_legacy(agentic)
    assert {"type": "budget", "max": 3} in rules
    assert {"type": "errors", "max": 0} in rules
    assert {"type": "sequence", "steps": ["retrieval", "lookup"], "mode": "in_order_exact"} in rules
    assert {"type": "retrieval", "min_documents": 1, "sources": ["policy.md"]} in rules
    assert rest == {}


def test_forbidden_data_scopes_become_forbidden_values():
    rules, _ = split_legacy({"policy": {"forbidden_data_scopes": ["ACME-1002"]}})
    assert rules == [{"type": "forbidden_values", "values": ["ACME-1002"]}]


def test_unmapped_settings_stay_in_place():
    agentic = {
        "tools": {"allowed": ["lookup"], "expected": [{"name": "lookup"}]},
        "policy": {"read_only": True, "allowed_data_scopes": ["x"]},
        "approvals": {"required_for": ["refund"]},
        "semantic_judge": {"enabled": True, "criteria": ["grounding"]},
    }
    rules, rest = split_legacy(agentic)
    assert [r["type"] for r in rules] == ["tool_called"]
    assert rest["tools"] == {"allowed": ["lookup"]}
    assert rest["policy"] == {"read_only": True, "allowed_data_scopes": ["x"]}
    assert rest["approvals"] and rest["semantic_judge"]


def test_editor_shows_saved_rules_then_converted_legacy():
    agentic = {"rules": [{"type": "errors", "max": 1}], "budgets": {"max_tool_calls": 2}}
    assert editor_rules(agentic) == [{"type": "errors", "max": 1}, {"type": "budget", "max": 2}]
    assert has_legacy(agentic)
    assert not has_legacy({"rules": [{"type": "errors", "max": 1}]})
