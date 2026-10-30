"""Agentic rules. Every rule is a custom check over trace evidence (see rule_engine).

``STARTERS`` are editor templates: each one is a list of ordinary custom checks that
the editor adds to the rule list. They are data, not a second rule type.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuleError:
    index: int
    rule_type: str
    message: str


STARTERS: list[dict[str, Any]] = [
    {"group": "Tools", "label": "Tool must be called", "rules": [
        {"type": "check", "select": {"kind": "tool", "where": [
            {"field": "tool.name", "op": "equals", "value": "acme_get_order"},
            {"field": "tool.argument.order_id", "op": "equals", "value": "ACME-1001"}]}, "check": "count",
         "expect": {"op": "at_least", "value": 1}}]},
    {"group": "Tools", "label": "Tool must not be called", "rules": [
        {"type": "check", "select": {"kind": "tool", "where": [
            {"field": "tool.name", "op": "equals", "value": "acme_create_refund_request"},
            {"field": "tool.argument.order_id", "op": "equals", "value": "ACME-1001"}]}, "check": "none"}]},
    {"group": "Tools", "label": "Limit tool calls", "rules": [
        {"type": "check", "select": {"kind": "tool", "where": [
            {"field": "tool.name", "op": "equals", "value": "acme_get_order"}]}, "check": "count",
         "expect": {"op": "at_most", "value": 3}}]},
    {"group": "Tools", "label": "These tools must each be called", "rules": [
        {"type": "check", "select": {"kind": "tool", "where": [
            {"field": "tool.name", "op": "equals", "value": "acme_get_order"},
            {"field": "tool.argument.order_id", "op": "equals", "value": "ACME-1001"}]}, "check": "count",
         "expect": {"op": "at_least", "value": 1}},
        {"type": "check", "select": {"kind": "tool", "where": [
            {"field": "tool.name", "op": "equals", "value": "acme_create_refund_request"},
            {"field": "tool.argument.order_id", "op": "equals", "value": "ACME-1001"}]}, "check": "count",
         "expect": {"op": "at_least", "value": 1}}]},
    {"group": "Answer and values", "label": "Forbidden value must not appear in tool results", "rules": [
        {"type": "check", "select": {"kind": "tool", "where": [
            {"field": "tool.result", "op": "contains", "value": "USB-C Charger"}]}, "check": "none"}]},
    {"group": "Answer and values", "label": "Forbidden value must not appear in the answer", "rules": [
        {"type": "check", "select": {"kind": "answer", "where": [
            {"field": "answer.text", "op": "contains", "value": "USB-C Charger"}]}, "check": "none"}]},
    {"group": "Retrieval", "label": "Must retrieve from knowledge", "rules": [
        {"type": "check", "select": {"kind": "retrieval_document", "where": []}, "check": "count",
         "expect": {"op": "at_least", "value": 1}}]},
    {"group": "Retrieval", "label": "Must retrieve a named source", "rules": [
        {"type": "check", "select": {"kind": "retrieval_document", "where": [
            {"field": "retrieval.source", "op": "equals", "value": "customer_faq.md"}]}, "check": "exists"}]},
    {"group": "Retrieval", "label": "Must retrieve any of these sources", "rules": [
        {"type": "check", "select": {"kind": "retrieval_document", "where": [
            {"field": "retrieval.source", "op": "one_of", "value": ["customer_faq.md", "policy.md"]}]}, "check": "exists"}]},
    {"group": "Errors", "label": "Limit error steps", "rules": [
        {"type": "check", "select": {"kind": "any", "where": [
            {"field": "span.error", "op": "equals", "value": True}]}, "check": "count",
         "expect": {"op": "at_most", "value": 0}}]},
]


def validate_rules(rules: Any) -> list[RuleError]:
    """Validation problems for a rule list. An empty list means every rule is valid."""
    from .rule_engine import validate_check

    if not isinstance(rules, list):
        return [RuleError(-1, "", "rules must be a list")]
    errors: list[RuleError] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(RuleError(index, "", "each rule must be an object"))
        elif rule.get("type") != "check":
            errors.append(RuleError(index, str(rule.get("type", "")), "rules must be custom checks"))
        else:
            errors.extend(RuleError(index, "check", problem) for problem in validate_check(rule))
    return errors


def describe_rule(rule: dict) -> str:
    """One readable sentence for a rule."""
    from .rule_engine import describe_check

    return describe_check(rule)


def check_label(check_id: str) -> str:
    """Readable name for a check id; rule checks read as 'Rule N · Custom check'."""
    parts = check_id.split(".")
    if len(parts) == 3 and parts[0] == "rule" and parts[1].isdigit() and parts[2] == "check":
        return f"Rule {int(parts[1]) + 1} · Custom check"
    return check_id
