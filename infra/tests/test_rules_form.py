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
