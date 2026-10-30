"""Structured agentic rules: one fixed set of rule types, each checked from the trace.

A rule is a JSON object with a ``type`` and the fields of that type. This module
only defines and validates the shapes; the checks that evaluate them come later.
Every rule type reads trace evidence only (tool spans, retrieval steps, model
spans, span order), never scenario or tool identities.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SEQUENCE_MODES = (
    "in_order_others_allowed",  # listed steps in this order; other steps allowed (default)
    "in_order_exact",           # exactly these steps, in this order, nothing else
    "any_order_exact",          # exactly these steps, any order, nothing else
    "any_order_others_allowed",  # listed steps present in any order; others allowed
)
ANSWER_SCOPES = ("arguments", "results", "answer")


@dataclass(frozen=True)
class RuleError:
    index: int
    rule_type: str
    message: str


def _is_name(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_fields(rule: dict, required: dict[str, Callable[[Any], bool]], optional: dict[str, Callable[[Any], bool]]) -> list[str]:
    problems = []
    for key, valid in required.items():
        if key not in rule:
            problems.append(f"'{key}' is required")
        elif not valid(rule[key]):
            problems.append(f"'{key}' has an invalid value")
    for key, valid in optional.items():
        if key in rule and not valid(rule[key]):
            problems.append(f"'{key}' has an invalid value")
    unknown = set(rule) - {"type"} - set(required) - set(optional)
    if unknown:
        problems.append(f"unknown field(s): {', '.join(sorted(unknown))}")
    return problems


def _names(value: Any) -> bool:
    return isinstance(value, list) and value and all(_is_name(item) for item in value)


def _arguments(value: Any) -> bool:
    return isinstance(value, dict) and all(_is_name(key) for key in value)


def _scopes(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(item in ANSWER_SCOPES for item in value)


# type -> (required fields, optional fields); each field maps to a validator.
RULE_TYPES: dict[str, tuple[dict[str, Callable[[Any], bool]], dict[str, Callable[[Any], bool]]]] = {
    "tool_called": (
        {"tool": _is_name},
        {"arguments": _arguments, "min": _is_count, "max": _is_count},
    ),
    "tool_not_called": (
        {"tool": _is_name},
        {"arguments": _arguments},
    ),
    "sequence": (
        {"steps": _names},
        {"mode": lambda v: v in SEQUENCE_MODES},
    ),
    "precedes": (
        {"first": _is_name, "then": _is_name},
        {},
    ),
    "budget": (
        {"max": _is_count},
        {"tool": _is_name},
    ),
    "repeated_calls": (
        {"max_identical": _is_count},
        {},
    ),
    "forbidden_values": (
        {"values": _names},
        {"scope": _scopes},
    ),
    "grounded_value": (
        {"tool": _is_name, "field": _is_name},
        {},
    ),
    "retrieval": (
        {},
        {"min_documents": _is_count, "sources": _names},
    ),
    "errors": (
        {"max": _is_count},
        {},
    ),
    "retries": (
        {"tool": _is_name, "max_retries": _is_count},
        {"same_arguments": lambda v: isinstance(v, bool)},
    ),
}


def validate_rules(rules: Any) -> list[RuleError]:
    """Validation problems for a rule list. An empty list means every rule is valid."""
    if not isinstance(rules, list):
        return [RuleError(-1, "", "rules must be a list")]
    errors: list[RuleError] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(RuleError(index, "", "each rule must be an object"))
            continue
        rule_type = rule.get("type")
        if rule_type not in RULE_TYPES:
            errors.append(RuleError(index, str(rule_type), f"unknown rule type {rule_type!r}"))
            continue
        required, optional = RULE_TYPES[rule_type]
        for problem in _check_fields(rule, required, optional):
            errors.append(RuleError(index, rule_type, problem))
        if rule_type == "tool_called" and "min" in rule and "max" in rule and rule["min"] > rule["max"]:
            errors.append(RuleError(index, rule_type, "'min' is larger than 'max'"))
    return errors


def normalize_rules(rules: list[dict]) -> list[dict]:
    """Rules with defaults filled in. Call only after validate_rules returns no errors."""
    out = []
    for rule in rules:
        item = dict(rule)
        if item["type"] == "sequence":
            item.setdefault("mode", "in_order_others_allowed")
        if item["type"] == "forbidden_values":
            item.setdefault("scope", list(ANSWER_SCOPES))
        if item["type"] == "retries":
            item.setdefault("same_arguments", True)
        out.append(item)
    return out
