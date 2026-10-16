"""Sparse-schema v2 tests: absence is not an assertion, presence is strict."""
import json
from pathlib import Path

import pytest

from audits.agentic.schema_v2 import (
    get_schema_v2_template,
    migrate_v1_to_v2,
    validate_agentic_metadata,
)

_CATALOG = Path(__file__).resolve().parents[3] / "infra" / "fixtures" / "agentic_demo_catalog_v3.json"


def _ok(metadata):
    valid, errors = validate_agentic_metadata(metadata)
    assert valid, errors
    return metadata


def _bad(metadata):
    valid, errors = validate_agentic_metadata(metadata)
    assert not valid, f"expected rejection: {metadata}"
    return errors


def test_bare_v2_document_is_valid():
    _ok({"schema_version": 2})


def test_every_optional_section_can_be_omitted():
    _ok({
        "schema_version": 2,
        "trace": {"required": True, "missing_evidence": "inconclusive"},
        "enforcement": {"mode": "gating"},
        "tools": {"expected": [{"name": "acme_get_order", "min_calls": 1}]},
    })
    # none of the omitted sections appear as implicit assertions
    assert validate_agentic_metadata(_ok({"schema_version": 2}))[0]


@pytest.mark.parametrize("section", [
    "tools", "retrieval", "trajectory", "policy", "budgets", "approvals",
    "handoffs", "guardrails", "rerank", "semantic_judge", "state",
])
def test_each_section_absent_or_empty_is_valid(section):
    _ok({"schema_version": 2})
    _ok({"schema_version": 2, section: {}})


def test_explicit_false_zero_null_are_assertions_not_errors():
    _ok({
        "schema_version": 2,
        "retrieval": {"required": False, "min_documents": 0, "max_documents": None},
        "policy": {"read_only": False, "allow_side_effects": False},
        "approvals": {"required_for": [], "must_precede_execution": False},
    })


def test_missing_evidence_keeps_inconclusive_semantics():
    _ok({"schema_version": 2, "trace": {"missing_evidence": "inconclusive"}})
    errors = _bad({"schema_version": 2, "trace": {"missing_evidence": "explode"}})
    assert any("missing_evidence" in e for e in errors)


@pytest.mark.parametrize("metadata,needle", [
    ({"schema_version": 2, "tools": {"expected": "lookup"}}, "tools.expected"),
    ({"schema_version": 2, "tools": {"forbidden": "x"}}, "tools.forbidden"),
    ({"schema_version": 2, "tools": {"max_total_calls": -1}}, "tools.max_total_calls"),
    ({"schema_version": 2, "tools": {"expected": [{"name": "a", "min_calls": "2"}]}}, "min_calls"),
    ({"schema_version": 2, "tools": {"expected": [{"max_calls": 1}]}}, ".name"),
    ({"schema_version": 2, "tools": {"expected": [{"name": "a", "arguments_mode": "some"}]}}, "arguments_mode"),
    ({"schema_version": 2, "tools": {"expected": [{"name": "a", "arguments": []}]}}, "arguments"),
    ({"schema_version": 2, "retrieval": {"sources": "kb"}}, "retrieval.sources"),
    ({"schema_version": 2, "retrieval": {"sources": [42]}}, "retrieval.sources[0]"),
    ({"schema_version": 2, "retrieval": {"min_documents": -2}}, "retrieval.min_documents"),
    ({"schema_version": 2, "retrieval": {"min_documents": "three"}}, "retrieval.min_documents"),
    ({"schema_version": 2, "trajectory": {"order_mode": "shuffled"}}, "order_mode"),
    ({"schema_version": 2, "trajectory": {"max_steps": "many"}}, "max_steps"),
    ({"schema_version": 2, "handoffs": {"max_handoffs": -1}}, "max_handoffs"),
    ({"schema_version": 2, "handoffs": {"required": ["A", 1]}}, "handoffs.required[1]"),
    ({"schema_version": 2, "budgets": {"max_tool_calls": -1}}, "budgets.max_tool_calls"),
    ({"schema_version": 2, "budgets": {"max_cost_usd": -0.5}}, "max_cost_usd"),
    ({"schema_version": 2, "semantic_judge": {"enabled": "yes"}}, "semantic_judge.enabled"),
    ({"schema_version": 2, "semantic_judge": {"criteria": "grounding"}}, "criteria"),
    ({"schema_version": 2, "policy": {"read_only": "yes"}}, "policy.read_only"),
    ({"schema_version": 2, "policy": {"allowed_data_scopes": "order:id"}}, "allowed_data_scopes"),
    ({"schema_version": 2, "approvals": {"required_for": "refund"}}, "required_for"),
    ({"schema_version": 2, "approvals": {"must_precede_execution": 1}}, "must_precede_execution"),
    ({"schema_version": 2, "guardrails": {"before_actions": "json?"}}, "before_actions"),
    ({"schema_version": 2, "state": {"assertions": {}}}, "assertions"),
    ({"schema_version": 2, "rerank": {"required": 1, "min_calls": -1}}, "rerank"),
    ({"schema_version": 2, "enforcement": {"mode": "yolo"}}, "enforcement.mode"),
    ({"schema_version": 2, "enforcement": {"deterministic_fail_severity": "apocalyptic"}}, "deterministic_fail_severity"),
    ({"schema_version": 2, "enforcement": {"severity_overrides": {"x": "apocalyptic"}}}, "severity_overrides.x"),
    ({"schema_version": 2, "trace": {"content_capture": "everything"}}, "content_capture"),
    ({"schema_version": 2, "retrieval": {"content_capture": "full"}}, "retrieval.content_capture"),
    ({"schema_version": 2, "goal": {"reference_outcome": 7}}, "reference_outcome"),
    ({"schema_version": 2, "tools": "not-a-dict"}, "tools must be a dict"),
])
def test_malformed_supplied_sections_are_rejected(metadata, needle):
    errors = _bad(metadata)
    assert any(needle in e for e in errors), (needle, errors)


def test_unknown_fields_still_rejected():
    errors = _bad({"schema_version": 2, "typo": True})
    assert "Unknown field: typo" in errors
    errors = _bad({"schema_version": 2, "tools": {"nope": []}})
    assert "Unknown field: tools.nope" in errors


def test_template_is_minimal_and_valid():
    template = get_schema_v2_template()
    _ok(template)
    # sparse: no dormant sections are pre-filled
    assert set(template) == {"schema_version", "enforcement"}


def test_migrate_v1_carries_only_present_sections():
    original = {"schema_version": 1, "trace": {"required": False}, "tools": {"forbidden": ["x"]}}
    migrated = migrate_v1_to_v2(original)
    assert set(migrated) == {"schema_version", "trace", "tools"}
    _ok(migrated)
    # never mutates the input
    migrated["trace"]["required"] = True
    assert original["trace"]["required"] is False


def test_catalog_v3_metadata_all_validate():
    catalog = json.loads(_CATALOG.read_text())
    keys = [s["key"] for s in catalog["scenarios"]]
    assert len(keys) >= 10
    for scenario in catalog["scenarios"]:
        _ok(scenario["metadata"]["agentic"])
