"""The scenario form saves structured rules: read, validated, cleared."""
import json

import pytest
from django.http import QueryDict

from infra.ui import _merge_structured_agentic_fields


def _post(**fields):
    qd = QueryDict(mutable=True)
    for key, value in fields.items():
        qd[key] = value
    return qd


def test_valid_rules_are_saved():
    rules = [{'type': 'check', 'select': {'kind': 'tool', 'where': [{'field': 'tool.name', 'op': 'equals', 'value': 'lookup'}]}, 'check': 'count', 'expect': {'op': 'at_least', 'value': 1}}]
    merged = _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps(rules)))
    assert merged["agentic"]["rules"] == rules


def test_invalid_rules_are_rejected_with_the_rule_number():
    with pytest.raises(ValueError, match="rule 1: rules must be custom checks"):
        _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([{"type": "tool_called"}])))


def test_malformed_json_is_rejected():
    with pytest.raises(json.JSONDecodeError):
        _merge_structured_agentic_fields({}, _post(agentic_rules_json="[{"))


def test_cleared_editor_removes_rules_saved_earlier():
    prior = {"agentic": {"schema_version": 2, "rules": [{'type': 'check', 'select': {'kind': 'any', 'where': [{'field': 'span.error', 'op': 'equals', 'value': True}]}, 'check': 'count', 'expect': {'op': 'at_most', 'value': 0}}]}}
    merged = _merge_structured_agentic_fields(prior, _post(agentic_rules_json=""))
    assert "rules" not in merged["agentic"]


def test_empty_editor_on_untouched_scenario_changes_nothing():
    untouched = {"name": "x"}
    assert _merge_structured_agentic_fields(untouched, _post(agentic_rules_json="")) == untouched


def test_custom_check_built_in_the_editor_is_saved():
    rule = {"type": "check", "select": {"kind": "tool", "where": [{"field": "tool.name", "op": "equals", "value": "lookup"}]},
            "check": "count", "expect": {"op": "at_least", "value": 1}}
    merged = _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([rule])))
    assert merged["agentic"]["rules"] == [rule]


def test_unsupported_rule_from_the_migration_is_saved_unchanged_beside_a_new_rule():
    unsupported = {"type": "check", "check": "unsupported", "legacy": {"type": "precedes", "first": "a", "then": "b"}}
    rule = {"type": "check", "select": {"kind": "tool", "where": []}, "check": "count", "expect": {"op": "at_least", "value": 1}}
    merged = _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([unsupported, rule])))
    assert merged["agentic"]["rules"] == [unsupported, rule]


_VALID = {"type": "check", "select": {"kind": "tool", "where": []}, "check": "count", "expect": {"op": "at_least", "value": 1}}


@pytest.mark.parametrize(("rule", "message"), [
    ({"type": "check", "check": "count", "expect": {"op": "at_least", "value": 2}}, "'select.kind' must be one of"),
    ({"type": "check", "select": {"kind": "tool", "where": 5}, "check": "count", "expect": {"op": "at_least", "value": 1}},
     "'select.where' must be a list"),
    ({"type": "check", "select": {"kind": "tool", "where": None}, "check": "count", "expect": {"op": "at_least", "value": 1}},
     "'select.where' must be a list"),
])
def test_rule_the_form_cannot_edit_is_rejected_on_save_with_its_number(rule, message):
    # The editor keeps such a rule as typed under "Edit as JSON"; saving names it.
    with pytest.raises(ValueError, match=f"rule 2: {message}"):
        _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([_VALID, rule])))


def test_not_one_of_saved_by_the_old_editor_is_still_accepted():
    # Rules stored before the editor split not_one_of values must stay valid.
    rule = {"type": "check", "select": {"kind": "tool", "where": [
        {"field": "tool.name", "op": "not_one_of", "value": "lookup,refund"}]}, "check": "none"}
    merged = _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([rule])))
    assert merged["agentic"]["rules"] == [rule]


def test_between_condition_without_two_numbers_is_rejected_on_save():
    # The old editor turned "1,5" into NaN, which JSON stores as null.
    rule = {"type": "check", "select": {"kind": "tool", "where": [
        {"field": "tool.argument.n", "op": "between", "value": None}]}, "check": "exists"}
    with pytest.raises(ValueError, match="rule 2: condition 1: 'between' needs two numbers, low first"):
        _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([_VALID, rule])))


@pytest.mark.parametrize(("condition_value", "expect", "message"), [
    ("abc", {"op": "at_least", "value": 1}, "condition 1: 'at_least' needs a number"),
    (2, {"op": "at_least", "value": None}, "'expect' needs a count of 0 or more"),
])
def test_numeric_value_the_old_editor_stored_as_text_or_null_is_rejected_on_save(condition_value, expect, message):
    rule = {"type": "check", "select": {"kind": "tool", "where": [
        {"field": "tool.argument.n", "op": "at_least", "value": condition_value}]}, "check": "count", "expect": expect}
    with pytest.raises(ValueError, match=f"rule 2: {message}"):
        _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([_VALID, rule])))
