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


def _cond(field: str, op: str, value: Any) -> dict:
    return {"field": field, "op": op, "value": value}


def _custom(kind: str, where: list[dict], check: str, expect: dict | None = None, **extra) -> dict:
    rule: dict[str, Any] = {"type": "check", "select": {"kind": kind, "where": where}, "check": check}
    if expect is not None:
        rule["expect"] = expect
    rule.update(extra)
    return rule


def _extra_rules(rest: dict, removed: list[tuple[str, str]]) -> list[dict]:
    """Settings the trace can check, as custom check rules. Each removed setting is recorded."""
    rules: list[dict] = []
    tools = rest.get("tools") or {}
    if tools.get("allowed"):
        rules.append(_custom("tool", [_cond("tool.name", "not_one_of", list(tools["allowed"]))], "none"))
        removed.append(("tools", "allowed"))
    retrieval = rest.get("retrieval") or {}
    if retrieval.get("max_documents") is not None:
        rules.append(_custom("retrieval_document", [], "count", {"op": "at_most", "value": retrieval["max_documents"]}))
        removed.append(("retrieval", "max_documents"))
    rerank = rest.get("rerank") or {}
    rerank_where = [_cond("span.kind", "equals", "rerank")]
    if rerank.get("required"):
        rules.append(_custom("any", rerank_where, "exists"))
        removed.append(("rerank", "required"))
    if rerank.get("min_calls") is not None:
        rules.append(_custom("any", rerank_where, "count", {"op": "at_least", "value": rerank["min_calls"]}))
        removed.append(("rerank", "min_calls"))
    if rerank.get("max_calls") is not None:
        rules.append(_custom("any", rerank_where, "count", {"op": "at_most", "value": rerank["max_calls"]}))
        removed.append(("rerank", "max_calls"))
    trajectory = rest.get("trajectory") or {}
    if trajectory.get("max_steps") is not None:
        rules.append(_custom("any", [], "count", {"op": "at_most", "value": trajectory["max_steps"]}))
        removed.append(("trajectory", "max_steps"))
    if trajectory.get("max_identical_consecutive_calls") is not None:
        rules.append(_custom("tool", [], "max_run", {"op": "at_most", "value": trajectory["max_identical_consecutive_calls"]},
                             group_by=["tool.name", "tool.arguments"]))
        removed.append(("trajectory", "max_identical_consecutive_calls"))
    policy = rest.get("policy") or {}
    for name in policy.get("forbidden_side_effects") or []:
        rules.append({"type": "tool_not_called", "tool": name})
    if policy.get("forbidden_side_effects"):
        removed.append(("policy", "forbidden_side_effects"))
    handoffs = rest.get("handoffs") or {}
    for name in handoffs.get("required") or []:
        rules.append(_custom("any", [_cond("span.kind", "equals", "handoff"), _cond("span.name", "equals", name)], "exists"))
    if handoffs.get("required"):
        removed.append(("handoffs", "required"))
    if handoffs.get("max_handoffs") is not None:
        rules.append(_custom("any", [_cond("span.kind", "equals", "handoff")], "count",
                             {"op": "at_most", "value": handoffs["max_handoffs"]}))
        removed.append(("handoffs", "max_handoffs"))
    return rules


def _drop(rest: dict, section: str, key: str) -> None:
    if isinstance(rest.get(section), dict):
        rest[section].pop(key, None)


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
    removed: list[tuple[str, str]] = []
    rules = (
        _tool_rules(tools) + _budget_rules(budgets, tools) + _sequence_rules(trajectory)
        + _retrieval_rules(retrieval) + _forbidden_value_rules(policy)
    )
    rules += _extra_rules(rest, removed)
    for section, key in removed:
        _drop(rest, section, key)
    rest_section_keys = ("tools", "budgets", "trajectory", "retrieval", "policy", "rerank", "handoffs")
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
    for section in rest_section_keys:
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
