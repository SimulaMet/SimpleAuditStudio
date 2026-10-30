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
    rules = [{"type": "tool_called", "tool": "lookup", "min": 1}]
    merged = _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps(rules)))
    assert merged["agentic"]["rules"] == rules


def test_invalid_rules_are_rejected_with_the_rule_number():
    with pytest.raises(ValueError, match="rule 1: 'tool' is required"):
        _merge_structured_agentic_fields({}, _post(agentic_rules_json=json.dumps([{"type": "tool_called"}])))


def test_malformed_json_is_rejected():
    with pytest.raises(json.JSONDecodeError):
        _merge_structured_agentic_fields({}, _post(agentic_rules_json="[{"))


def test_cleared_editor_removes_rules_saved_earlier():
    prior = {"agentic": {"schema_version": 2, "rules": [{"type": "errors", "max": 0}]}}
    merged = _merge_structured_agentic_fields(prior, _post(agentic_rules_json=""))
    assert "rules" not in merged["agentic"]


def test_empty_editor_on_untouched_scenario_changes_nothing():
    untouched = {"name": "x"}
    assert _merge_structured_agentic_fields(untouched, _post(agentic_rules_json="")) == untouched


def test_saving_takes_over_legacy_settings_that_were_converted():
    prior = {"agentic": {"schema_version": 2, "budgets": {"max_tool_calls": 3}, "approvals": {"required_for": ["refund"]}}}
    rules = [{"type": "budget", "max": 3}]
    post = _post(agentic_rules_json=json.dumps(rules), agentic_approvals_required_for="refund")
    merged = _merge_structured_agentic_fields(prior, post)
    assert merged["agentic"]["rules"] == rules
    assert "budgets" not in merged["agentic"]
    assert merged["agentic"]["approvals"]["required_for"] == ["refund"]


def test_untouched_editor_on_legacy_scenario_saves_the_converted_rules():
    prior = {"agentic": {"schema_version": 2, "budgets": {"max_tool_calls": 3}}}
    shown = [{"type": "budget", "max": 3}]
    merged = _merge_structured_agentic_fields(prior, _post(agentic_rules_json=json.dumps(shown)))
    assert merged["agentic"]["rules"] == shown
    assert "budgets" not in merged["agentic"]
