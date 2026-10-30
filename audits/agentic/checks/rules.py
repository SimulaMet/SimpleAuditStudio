"""Evaluate structured rules (audits/agentic/rules.py) from the trace.

Each rule yields one check, ``rule.<index>.<type>``. Rules read only trace
evidence: tool calls and their arguments and results, retrieval steps, error
status, and the final answer (the last model output). Missing evidence is
INCONCLUSIVE; an empty trace makes every rule INCONCLUSIVE.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from ..schema import AgentTrajectory
from .base import CheckResult, result
from .policy import _as_text, _final_answer
from .retrieval import _captured_tool_sources, _retrieval_steps, _source_values
from .tools import _arguments_match, _tool_arguments

_ACTION_KINDS = {"tool", "retrieval", "approval", "handoff", "rerank", "guardrail"}


def _label(step) -> str:
    return step.name if step.kind == "tool" else step.kind


def _actions(trajectory: AgentTrajectory) -> list:
    """Action steps only (tools, retrieval, approvals, ...), so model steps do not
    interfere with sequences."""
    return [s for s in trajectory.primary_steps if s.kind in _ACTION_KINDS]


def _tool_calls(trajectory: AgentTrajectory, tool: str | None = None) -> list:
    return [s for s in trajectory.steps if s.kind == "tool" and (tool is None or s.name == tool)]


def _matches(step, tool: str, arguments: dict | None) -> bool:
    if step.name != tool:
        return False
    return not arguments or _arguments_match(_tool_arguments(step) or {}, arguments)


def _result_value(step) -> Any:
    raw = step.result if step.result is not None else step.attributes.get("gen_ai.tool.call.result")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw


def _field(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _tool_call_rule(index, rule, trajectory):
    calls = [s for s in _tool_calls(trajectory, rule["tool"]) if _matches(s, rule["tool"], rule.get("arguments"))]
    low = rule.get("min", 0 if "max" in rule else 1)
    high = rule.get("max")
    ok = len(calls) >= low and (high is None or len(calls) <= high)
    bound = f"at least {low}" + (f", at most {high}" if high is not None else "")
    return result(f"rule.{index}.tool_called", "rules", "PASS" if ok else "FAIL",
                  f"{rule['tool']} called {len(calls)} time(s); required {bound}.",
                  expected=bound, observed=len(calls),
                  evidence_span_ids=[s.span_id for s in calls if s.span_id])


def _tool_not_called_rule(index, rule, trajectory):
    bad = [s for s in _tool_calls(trajectory, rule["tool"]) if _matches(s, rule["tool"], rule.get("arguments"))]
    return result(f"rule.{index}.tool_not_called", "rules", "FAIL" if bad else "PASS",
                  f"{rule['tool']} was called." if bad else f"{rule['tool']} was not called.",
                  observed=len(bad), evidence_span_ids=[s.span_id for s in bad if s.span_id])


def _sequence_ok(mode: str, wanted: list[str], labels: list[str]) -> bool:
    if mode == "in_order_exact":
        return labels == wanted
    if mode == "any_order_exact":
        return Counter(labels) == Counter(wanted)
    if mode == "any_order_others_allowed":
        return not (Counter(wanted) - Counter(labels))
    position = 0  # in_order_others_allowed: subsequence
    for label in labels:
        if position < len(wanted) and label == wanted[position]:
            position += 1
    return position == len(wanted)


def _sequence_rule(index, rule, trajectory):
    labels = [_label(s) for s in _actions(trajectory)]
    mode = rule.get("mode", "in_order_others_allowed")
    ok = _sequence_ok(mode, rule["steps"], labels)
    return result(f"rule.{index}.sequence", "rules", "PASS" if ok else "FAIL",
                  f"Actions were {' > '.join(labels) or 'none'}; required {mode.replace('_', ' ')} {' > '.join(rule['steps'])}.",
                  expected=rule["steps"], observed=labels)


def _precedes_rule(index, rule, trajectory):
    actions = _actions(trajectory)
    first = [i for i, s in enumerate(actions) if _label(s) == rule["first"]]
    then = [i for i, s in enumerate(actions) if _label(s) == rule["then"]]
    if not then:
        return result(f"rule.{index}.precedes", "rules", "PASS",
                      f"{rule['then']} did not occur, so nothing needed {rule['first']} first.")
    ok = bool(first) and min(first) < min(then)
    return result(f"rule.{index}.precedes", "rules", "PASS" if ok else "FAIL",
                  f"{rule['first']} comes before {rule['then']}." if ok
                  else f"{rule['then']} occurred without an earlier {rule['first']}.",
                  expected=f"{rule['first']} before {rule['then']}", observed=[_label(s) for s in actions])


def _budget_rule(index, rule, trajectory):
    count = len(_tool_calls(trajectory, rule.get("tool")))
    ok = count <= rule["max"]
    scope = rule.get("tool") or "all tools"
    return result(f"rule.{index}.budget", "rules", "PASS" if ok else "FAIL",
                  f"{count} call(s) to {scope}; limit {rule['max']}.", expected=rule["max"], observed=count)


def _repeated_calls_rule(index, rule, trajectory):
    keys = Counter(
        (s.name, json.dumps(_tool_arguments(s) or {}, sort_keys=True)) for s in _tool_calls(trajectory)
    )
    worst = max(keys.values(), default=0)
    ok = worst <= rule["max_identical"]
    return result(f"rule.{index}.repeated_calls", "rules", "PASS" if ok else "FAIL",
                  f"Most identical repeats: {worst}; limit {rule['max_identical']}.",
                  expected=rule["max_identical"], observed=worst)


def _forbidden_values_rule(index, rule, trajectory):
    values = rule["values"]
    scope = rule.get("scope") or ["arguments", "results", "answer"]
    hits: list[str] = []
    if "arguments" in scope:
        for step in _tool_calls(trajectory):
            text = _as_text(_tool_arguments(step))
            hits += [f"{step.name} arguments: {v}" for v in values if v in text]
    if "results" in scope:
        for step in _tool_calls(trajectory):
            text = _as_text(_result_value(step))
            hits += [f"{step.name} result: {v}" for v in values if v in text]
    if "answer" in scope:
        answer = _final_answer(trajectory)
        hits += [f"answer: {v}" for v in values if v in answer]
    return result(f"rule.{index}.forbidden_values", "rules", "FAIL" if hits else "PASS",
                  "Forbidden value(s) found: " + "; ".join(hits) if hits else "No forbidden value found.",
                  expected=values, observed=hits)


def _grounded_value_rule(index, rule, trajectory):
    calls = [s for s in _tool_calls(trajectory, rule["tool"]) if _result_value(s) is not None]
    if not calls:
        return result(f"rule.{index}.grounded_value", "rules", "INCONCLUSIVE",
                      f"No {rule['tool']} result to ground the answer in.")
    found, value = _field(_result_value(calls[-1]), rule["field"])
    if not found:
        return result(f"rule.{index}.grounded_value", "rules", "INCONCLUSIVE",
                      f"{rule['tool']} result has no field '{rule['field']}'.")
    answer = _final_answer(trajectory)
    ok = str(value) in answer
    return result(f"rule.{index}.grounded_value", "rules", "PASS" if ok else "FAIL",
                  f"Answer {'contains' if ok else 'does not contain'} {rule['tool']}.{rule['field']} = {value}.",
                  expected=str(value), observed=answer[:200])


def _retrieval_rule(index, rule, trajectory):
    steps = _retrieval_steps(trajectory)
    low = rule.get("min_documents", 1)
    sources: set[str] = set()
    for step in steps:
        sources.update(_source_values(step.attributes.get("gen_ai.retrieval.documents")))
        if step.kind == "tool":
            sources.update(_captured_tool_sources(step))
    missing = [s for s in rule.get("sources", []) if s not in sources]
    ok = len(steps) >= low and not missing
    summary = f"{len(steps)} retrieval step(s); required at least {low}."
    if missing:
        summary += f" Missing source(s): {', '.join(missing)}."
    return result(f"rule.{index}.retrieval", "rules", "PASS" if ok else "FAIL", summary,
                  expected=rule.get("sources"), observed=sorted(sources))


def _errors_rule(index, rule, trajectory):
    errors = [s for s in trajectory.steps if s.is_error()]
    ok = len(errors) <= rule["max"]
    return result(f"rule.{index}.errors", "rules", "PASS" if ok else "FAIL",
                  f"{len(errors)} error step(s); limit {rule['max']}.",
                  expected=rule["max"], observed=len(errors),
                  evidence_span_ids=[s.span_id for s in errors if s.span_id])


def _retries_rule(index, rule, trajectory):
    calls = _tool_calls(trajectory, rule["tool"])
    failed = next((i for i, s in enumerate(calls) if s.is_error()), None)
    if failed is None:
        return result(f"rule.{index}.retries", "rules", "PASS", f"{rule['tool']} did not fail.")
    retries = calls[failed + 1:]
    failed_args = _tool_arguments(calls[failed]) or {}
    changed = [s for s in retries if (_tool_arguments(s) or {}) != failed_args]
    problems = []
    if len(retries) > rule["max_retries"]:
        problems.append(f"{len(retries)} retries; limit {rule['max_retries']}")
    if rule.get("same_arguments", True) and changed:
        problems.append("a retry used different arguments")
    return result(f"rule.{index}.retries", "rules", "FAIL" if problems else "PASS",
                  "; ".join(problems) if problems else f"{len(retries)} retry(ies), within limits.",
                  expected=rule["max_retries"], observed=len(retries),
                  evidence_span_ids=[s.span_id for s in retries if s.span_id])


_EVALUATORS = {
    "tool_called": _tool_call_rule,
    "tool_not_called": _tool_not_called_rule,
    "sequence": _sequence_rule,
    "precedes": _precedes_rule,
    "budget": _budget_rule,
    "repeated_calls": _repeated_calls_rule,
    "forbidden_values": _forbidden_values_rule,
    "grounded_value": _grounded_value_rule,
    "retrieval": _retrieval_rule,
    "errors": _errors_rule,
    "retries": _retries_rule,
}


def rule_checks(trajectory: AgentTrajectory, rules: list[dict]) -> list[CheckResult]:
    """One check per rule. Rules must already pass validate_rules."""
    from ..rules import normalize_rules

    out = []
    for index, rule in enumerate(normalize_rules(rules)):
        rule_type = rule["type"]
        if not trajectory.steps:
            out.append(result(f"rule.{index}.{rule_type}", "rules", "INCONCLUSIVE", "No trace evidence captured."))
            continue
        out.append(_EVALUATORS[rule_type](index, rule, trajectory))
    return out
