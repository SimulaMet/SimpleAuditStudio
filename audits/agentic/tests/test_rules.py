"""Agentic rules: every rule is one count, exists, none, all or distinct check."""
import pytest

from audits.agentic.rules import STARTERS, check_label, describe_rule, validate_rules


def _messages(rules):
    return [(e.index, e.message) for e in validate_rules(rules)]


def _error_check(**overrides):
    rule = {"type": "check", "select": {"kind": "any", "where": [
        {"field": "span.error", "op": "equals", "value": True}]}, "check": "count",
        "expect": {"op": "at_most", "value": 0}}
    rule.update(overrides)
    return rule


def test_valid_checks_pass():
    rules = [
        {'type': 'check', 'select': {'kind': 'tool', 'where': [{'field': 'tool.name', 'op': 'equals', 'value': 'lookup'}, {'field': 'tool.argument.id', 'op': 'equals', 'value': 'A'}]}, 'check': 'count', 'expect': {'op': 'at_least', 'value': 1}},
        {'type': 'check', 'select': {'kind': 'tool', 'where': [{'field': 'tool.name', 'op': 'equals', 'value': 'send_email'}]}, 'check': 'none'},
        {'type': 'check', 'select': {'kind': 'answer', 'where': [{'field': 'answer.text', 'op': 'contains', 'value': 'SECRET-42'}]}, 'check': 'none'},
        {'type': 'check', 'select': {'kind': 'retrieval_document', 'where': [{'field': 'retrieval.source', 'op': 'equals', 'value': 'policy.md'}]}, 'check': 'exists'},
        {'type': 'check', 'select': {'kind': 'tool', 'where': []}, 'check': 'all', 'expect': {'op': 'at_least', 'value': 1}},
        {'type': 'check', 'select': {'kind': 'tool', 'where': []}, 'check': 'distinct', 'field': 'tool.arguments', 'expect': {'op': 'at_most', 'value': 2}},
        _error_check(expect={"op": "between", "value": [0, 1]}),
    ]
    assert validate_rules(rules) == []


def test_presets_are_not_rules_any_more():
    assert _messages([{"type": "vibe_check"}]) == [(0, "rules must be custom checks")]
    assert _messages([{"type": "tool_called", "tool": "lookup"}]) == [(0, "rules must be custom checks")]


def test_removed_check_kinds_are_rejected():
    assert _messages([{"type": "check", "check": "in_order", "steps": []}]) == [
        (0, "unknown field(s): steps"), (0, "'check' must be one of count, exists, none, all, distinct")]


def test_wrong_value_type_is_reported():
    assert _messages([_error_check(expect={"op": "at_most", "value": "3"})]) == [
        (0, "'expect' needs a count of 0 or more")]


def test_negative_count_is_rejected():
    assert _messages([_error_check(expect={"op": "at_most", "value": -1})]) == [
        (0, "'expect' needs a count of 0 or more")]


def test_unknown_field_is_reported():
    assert _messages([_error_check(colour="red")]) == [(0, "unknown field(s): colour")]


def test_between_must_be_low_first():
    assert _messages([_error_check(expect={"op": "between", "value": [3, 1]})]) == [
        (0, "'between' needs two counts, low first")]


def test_errors_carry_the_rule_index():
    assert _messages([_error_check(), {"type": "budget"}]) == [(1, "rules must be custom checks")]


def test_rules_must_be_a_list_of_objects():
    assert validate_rules({"type": "check"})[0].message == "rules must be a list"
    assert _messages(["check"]) == [(0, "each rule must be an object")]


def test_describe_rule_reads_as_a_sentence():
    assert describe_rule({"type": "check", "select": {"kind": "tool", "where": [
        {"field": "tool.name", "op": "equals", "value": "acme_get_order"},
        {"field": "tool.argument.order_id", "op": "equals", "value": "ACME-1002"}]},
        "check": "count", "expect": {"op": "at_least", "value": 1}}) == (
        "acme_get_order(order_id='ACME-1002') must be called at least once")
    assert describe_rule(_error_check()) == "No error steps"
    assert describe_rule(_error_check(expect={"op": "at_most", "value": 1})) == "At most 1 error step"
    assert describe_rule({"type": "check", "select": {"kind": "retrieval_document", "where": []}, "check": "count",
                          "expect": {"op": "at_least", "value": 1}}) == "At least 1 retrieved document"


def test_describe_named_source_and_error_type_read_as_sentences():
    error = {"field": "span.error", "op": "equals", "value": True}
    error_type = {"field": "span.error_type", "op": "equals", "value": "ValueError"}
    assert describe_rule({"type": "check", "check": "exists", "select": {"kind": "any", "where": [error, error_type]}}) == (
        "At least one error where error type is ValueError occurs")
    assert describe_rule({"type": "check", "check": "exists", "select": {"kind": "retrieval_document", "where": [
        {"field": "retrieval.source", "op": "equals", "value": "customer_faq.md"}]}}) == (
        "At least one retrieved document where source is customer_faq.md occurs")


def test_check_label_names_rule_checks_and_leaves_others_alone():
    assert check_label("rule.0.check") == "Rule 1 · Custom check"
    assert check_label("trace.present") == "trace.present"


@pytest.mark.parametrize("starter", STARTERS, ids=lambda s: s["label"])
def test_every_starter_is_valid_custom_checks(starter):
    assert starter["rules"]
    assert validate_rules(starter["rules"]) == []
