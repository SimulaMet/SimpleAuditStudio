"""Agentic controls stored before the rule engine are kept as unsupported rules; none is dropped."""
import importlib

from audits.agentic.checks.rules import rule_checks
from audits.agentic.expectations import validate_agentic_metadata
from audits.agentic.trajectory import normalize

migration = importlib.import_module("scenarios.migrations.0005_convert_legacy_agentic_controls")

_BASE = {"schema_version": 2, "trace": {"required": True}, "enforcement": {"mode": "gating"}}

# D04 from infra/fixtures/agentic_demo_catalog_v3.json as it was before #65.
_D04 = {**_BASE,
        "tools": {"expected": [{"name": "acme_get_order", "min_calls": 1,
                                "arguments": {"order_id": "ACME-1004"}, "arguments_mode": "any"}]},
        "retrieval": {"required": True, "sources": ["Acme Retail Policy"]},
        "policy": {"read_only": True},
        "semantic_judge": {"enabled": True, "criteria": ["tool_result_handling", "grounding"]}}


def test_a_scenario_stored_before_the_rule_engine_validates_after_the_migration():
    metadata = migration.convert_metadata({"agentic": _D04})
    expectations = validate_agentic_metadata(metadata)
    assert expectations.rules == metadata["agentic"]["rules"]
    assert expectations.semantic_judge == _D04["semantic_judge"]


def test_each_legacy_section_is_kept_unchanged_and_reported_inconclusive():
    rules = migration.convert_metadata({"agentic": _D04})["agentic"]["rules"]
    assert [rule["legacy"] for rule in rules] == [
        {"type": "policy", "policy": _D04["policy"]},
        {"type": "retrieval", "retrieval": _D04["retrieval"]},
        {"type": "tools", "tools": _D04["tools"]},
    ]
    trace = normalize([{"span_id": "t1", "name": "execute_tool", "attributes": {
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "acme_get_order"}}])
    assert {check.status for check in rule_checks(trace, rules)} == {"INCONCLUSIVE"}


def test_rules_already_stored_come_first():
    stored = {"type": "check", "select": {"kind": "tool", "where": []}, "check": "none"}
    rules = migration.convert_metadata({"agentic": {**_BASE, "rules": [stored], "budgets": {"max_errors": 0}}})["agentic"]["rules"]
    assert rules[0] == stored and rules[1]["legacy"] == {"type": "budgets", "budgets": {"max_errors": 0}}


def test_metadata_in_the_current_shape_is_unchanged():
    metadata = {"agentic": {**_BASE, "rules": [
        {"type": "check", "select": {"kind": "tool", "where": []}, "check": "none"}]}}
    assert migration.convert_metadata(metadata) == metadata
