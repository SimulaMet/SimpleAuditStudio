"""Map legacy structured controls to rules, exactly where the meaning is the same.

Only settings with a rule equivalent are converted. Everything else (allowed
tool lists, approvals, handoffs, guardrails, read-only policy, judge settings,
...) stays in the legacy sections, so no check is lost.
"""
from __future__ import annotations

import copy
from typing import Any


def _tool_rules(tools: dict) -> list[dict]:
    rules: list[dict] = []
    for entry in tools.get("expected") or []:
        if isinstance(entry, str):
            rules.append({"type": "tool_called", "tool": entry, "min": 1})
            continue
        if not isinstance(entry, dict) or not entry.get("name") or entry.get("arguments_mode", "any") != "any":
            continue
        rule: dict[str, Any] = {"type": "tool_called", "tool": entry["name"]}
        if entry.get("arguments"):
            rule["arguments"] = entry["arguments"]
        rule["min"] = entry.get("min_calls", 1) if entry.get("min_calls") is not None else 0
        if entry.get("max_calls") is not None:
            rule["max"] = entry["max_calls"]
        rules.append(rule)
    for entry in tools.get("forbidden") or []:
        if isinstance(entry, str):
            rules.append({"type": "tool_not_called", "tool": entry})
        elif isinstance(entry, dict) and entry.get("name"):
            rule = {"type": "tool_not_called", "tool": entry["name"]}
            if entry.get("arguments"):
                rule["arguments"] = entry["arguments"]
            rules.append(rule)
    return rules


def _budget_rules(budgets: dict, tools: dict) -> list[dict]:
    rules = []
    limit = budgets.get("max_tool_calls", tools.get("max_total_calls"))
    if limit is not None:
        rules.append({"type": "budget", "max": limit})
    if budgets.get("max_errors") is not None:
        rules.append({"type": "errors", "max": budgets["max_errors"]})
    return rules


def _sequence_rules(trajectory: dict) -> list[dict]:
    steps = trajectory.get("required_sequence")
    if not steps:
        return []
    mode = "in_order_exact" if trajectory.get("order_mode") == "exact" else "in_order_others_allowed"
    return [{"type": "sequence", "steps": list(steps), "mode": mode}]


def _retrieval_rules(retrieval: dict) -> list[dict]:
    rule: dict[str, Any] = {"type": "retrieval"}
    if retrieval.get("min_documents") is not None:
        rule["min_documents"] = retrieval["min_documents"]
    if retrieval.get("sources"):
        rule["sources"] = list(retrieval["sources"])
    if len(rule) == 1 and not retrieval.get("required"):
        return []
    return [rule]


def _forbidden_value_rules(policy: dict) -> list[dict]:
    values = policy.get("forbidden_data_scopes")
    return [{"type": "forbidden_values", "values": list(values)}] if values else []


def split_legacy(agentic: dict) -> tuple[list[dict], dict]:
    """(rules derived from legacy sections, agentic with those settings removed)."""
    if not isinstance(agentic, dict):
        return [], {}
    rest = copy.deepcopy(agentic)
    tools = rest.get("tools") or {}
    budgets = rest.get("budgets") or {}
    trajectory = rest.get("trajectory") or {}
    retrieval = rest.get("retrieval") or {}
    policy = rest.get("policy") or {}
    rules = (
        _tool_rules(tools) + _budget_rules(budgets, tools) + _sequence_rules(trajectory)
        + _retrieval_rules(retrieval) + _forbidden_value_rules(policy)
    )
    # Remove exactly what was converted; keep the rest in place.
    for key in ("expected", "forbidden", "max_total_calls"):
        if tools.get(key) is not None:
            tools.pop(key, None)
    for key in ("max_tool_calls", "max_errors"):
        if budgets.get(key) is not None:
            budgets.pop(key, None)
    for key in ("required_sequence", "order_mode"):
        if trajectory.get(key) is not None:
            trajectory.pop(key, None)
    for key in ("sources", "required", "min_documents"):
        if retrieval.get(key) is not None:
            retrieval.pop(key, None)
    if policy.get("forbidden_data_scopes"):
        policy.pop("forbidden_data_scopes", None)
    for section in ("tools", "budgets", "trajectory", "retrieval", "policy"):
        if isinstance(rest.get(section), dict) and not rest[section]:
            rest.pop(section, None)
    return rules, rest


def has_legacy(agentic: dict) -> bool:
    return bool(split_legacy(agentic)[0])


def editor_rules(agentic: dict) -> list[dict]:
    """Rules shown in the editor: saved rules first, then settings converted from legacy."""
    saved = list((agentic or {}).get("rules") or [])
    converted, _ = split_legacy(agentic or {})
    return saved + converted
