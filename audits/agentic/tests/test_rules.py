"""Structured rule model: fixed rule types, validation messages, defaults."""
import pytest

from audits.agentic.rules import RULE_TYPES, normalize_rules, validate_rules


def _messages(rules):
    return [(e.index, e.message) for e in validate_rules(rules)]


def test_valid_rules_of_every_type_pass():
    rules = [
        {"type": "tool_called", "tool": "lookup", "arguments": {"id": "A"}, "min": 1},
        {"type": "tool_not_called", "tool": "send_email", "arguments": {"to": "x"}},
        {"type": "sequence", "steps": ["retrieval", "lookup"], "mode": "in_order_exact"},
        {"type": "precedes", "first": "approve", "then": "refund"},
        {"type": "budget", "max": 3, "tool": "lookup"},
        {"type": "repeated_calls", "max_identical": 1},
        {"type": "forbidden_values", "values": ["SECRET-42"], "scope": ["answer"]},
        {"type": "grounded_value", "tool": "lookup", "field": "status"},
        {"type": "retrieval", "min_documents": 1, "sources": ["policy.md"]},
        {"type": "errors", "max": 1},
        {"type": "retries", "tool": "lookup", "max_retries": 1, "same_arguments": True},
    ]
    assert {r["type"] for r in rules} == set(RULE_TYPES)
    assert validate_rules(rules) == []


def test_unknown_rule_type_is_reported():
    assert _messages([{"type": "vibe_check"}]) == [(0, "unknown rule type 'vibe_check'")]


def test_missing_required_field_is_reported():
    assert _messages([{"type": "tool_called"}]) == [(0, "'tool' is required")]


def test_wrong_value_type_is_reported():
    assert _messages([{"type": "budget", "max": "3"}]) == [(0, "'max' has an invalid value")]


def test_negative_count_is_rejected():
    assert _messages([{"type": "errors", "max": -1}]) == [(0, "'max' has an invalid value")]


def test_unknown_field_is_reported():
    assert _messages([{"type": "errors", "max": 0, "colour": "red"}]) == [(0, "unknown field(s): colour")]


def test_min_larger_than_max_is_reported():
    assert _messages([{"type": "tool_called", "tool": "t", "min": 3, "max": 1}]) == [
        (0, "'min' is larger than 'max'")
    ]


def test_sequence_mode_must_be_known():
    assert _messages([{"type": "sequence", "steps": ["a"], "mode": "random"}]) == [
        (0, "'mode' has an invalid value")
    ]


def test_forbidden_values_scope_must_be_known():
    assert _messages([{"type": "forbidden_values", "values": ["x"], "scope": ["logs"]}]) == [
        (0, "'scope' has an invalid value")
    ]


def test_rules_must_be_a_list_of_objects():
    assert validate_rules({"type": "errors"})[0].message == "rules must be a list"
    assert _messages(["errors"]) == [(0, "each rule must be an object")]


def test_errors_carry_the_rule_index():
    rules = [{"type": "errors", "max": 0}, {"type": "budget"}]
    assert _messages(rules) == [(1, "'max' is required")]


def test_normalize_fills_defaults():
    out = normalize_rules([
        {"type": "sequence", "steps": ["a", "b"]},
        {"type": "forbidden_values", "values": ["x"]},
        {"type": "retries", "tool": "t", "max_retries": 1},
    ])
    assert out[0]["mode"] == "in_order_others_allowed"
    assert out[1]["scope"] == ["arguments", "results", "answer"]
    assert out[2]["same_arguments"] is True


@pytest.mark.parametrize("rule", [
    {"type": "tool_called", "tool": "acme_get_order", "arguments": {"order_id": "ACME-1001"}, "min": 1},
    {"type": "grounded_value", "tool": "acme_get_order", "field": "delivered"},
    {"type": "retries", "tool": "acme_get_tracking_events", "max_retries": 1},
])
def test_scenario_style_rules_validate(rule):
    assert validate_rules([rule]) == []


def test_describe_rule_reads_as_a_sentence():
    from audits.agentic.rules import describe_rule

    assert describe_rule({"type": "tool_called", "tool": "lookup", "arguments": {"id": "A"}, "min": 1}) == (
        "Tool must be called (tool: lookup; arguments: id='A'; min: 1)")
    assert describe_rule({"type": "errors", "max": 0}) == "Limit error steps (max: 0)"
    assert describe_rule({"type": "retrieval"}) == "Must retrieve from knowledge"


def test_check_label_names_rule_checks_and_leaves_others_alone():
    from audits.agentic.rules import check_label

    assert check_label("rule.0.tool_called") == "Rule 1 · Tool must be called"
    assert check_label("trace.present") == "trace.present"
