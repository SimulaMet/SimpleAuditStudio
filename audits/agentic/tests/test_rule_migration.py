"""Stored rules convert to custom checks; what cannot convert is kept and marked, never dropped."""
import importlib

from audits.agentic.rules import validate_rules

migration = importlib.import_module("scenarios.migrations.0004_convert_rules_to_primitives")


def test_presets_convert_to_custom_checks_that_validate():
    presets = [
        {"type": "tool_called", "tool": "acme_get_order", "arguments": {"order_id": "ACME-1001"}, "min": 1},
        {"type": "tool_not_called", "tool": "acme_create_refund_request"},
        {"type": "budget", "max": 3, "tool": "acme_get_order"},
        {"type": "errors", "max": 0},
        {"type": "forbidden_values", "values": ["USB-C Charger"], "scope": ["results", "answer"]},
        {"type": "retrieval", "min_documents": 1, "sources": ["customer_faq.md"]},
        {"type": "sequence", "steps": ["a", "b"], "mode": "any_order_exact"},
    ]
    converted = migration.convert_rules(presets)
    assert all(rule["check"] in {"count", "exists", "none"} for rule in converted)
    assert validate_rules(converted) == []


def test_order_dependent_presets_are_kept_as_unsupported_with_the_original():
    precedes = {"type": "precedes", "first": "a", "then": "b"}
    converted = migration.convert_rules([precedes])
    assert converted == [{"type": "check", "check": "unsupported", "legacy": precedes}]


def test_all_of_groups_are_flattened_into_separate_rules():
    group = {"type": "check", "check": "all_of", "parts": [
        {"type": "check", "select": {"kind": "tool", "where": []}, "check": "none"},
        {"type": "check", "select": {"kind": "answer", "where": []}, "check": "none"},
    ]}
    assert [rule["select"]["kind"] for rule in migration.convert_rules([group])] == ["tool", "answer"]


def test_metadata_without_rules_is_unchanged():
    metadata = {"agentic": {"trace": {"required": True}}}
    assert migration.convert_metadata(metadata) == metadata


def test_unsupported_rules_kept_by_the_migration_pass_validation_and_are_inconclusive():
    from audits.agentic.checks.rules import rule_checks
    from audits.agentic.expectations import validate_agentic_metadata
    from audits.agentic.trajectory import normalize

    metadata = migration.convert_metadata({"agentic": {"schema_version": 2, "enforcement": {}, "rules": [
        {"type": "precedes", "first": "a", "then": "b"}]}})
    converted = metadata["agentic"]["rules"]
    assert validate_agentic_metadata(metadata).rules == converted
    trace = normalize([{"span_id": "t1", "name": "execute_tool", "attributes": {
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "a"}}])
    assert [check.status for check in rule_checks(trace, converted)] == ["INCONCLUSIVE"]


def test_only_the_migration_shape_of_an_unsupported_rule_skips_validation():
    junk = {"type": "check", "check": "unsupported", "select": "junk"}
    assert validate_rules([junk]) != []
