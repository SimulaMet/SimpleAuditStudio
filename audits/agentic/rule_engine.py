"""One rule engine: select trace units, then check them.

A custom rule reads as one sentence:
  look at <kind> units where <conditions>, check <count | exists | none | all | distinct>,
  expect <comparison>.

Units are read only from the trace: tool calls (name, arguments, result, error),
retrieval documents (source, text), the final answer, and any step. Presets
compile into this form, so there is one evaluator. Missing evidence is
INCONCLUSIVE.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .checks.base import CheckResult, result
from .checks.policy import _final_answer as final_answer
from .checks.retrieval import _retrieval_steps as retrieval_steps
from .checks.tools import _tool_arguments as tool_arguments
from .schema import AgentTrajectory

KINDS = ("tool", "retrieval_document", "answer", "any", "action")
CHECKS = ("count", "exists", "none", "all", "distinct", "in_order", "before", "max_group", "max_run")
ORDERING = ("in_order", "before", "max_group", "max_run")
ACTION_KINDS = {"tool", "retrieval", "approval", "handoff", "rerank", "guardrail"}
TEXT_OPS = ("equals", "not_equals", "one_of", "contains")
NUMBER_OPS = ("equals", "at_least", "at_most", "between")
EXPECT_OPS = ("equals", "at_least", "at_most", "between")

# Field name -> (kinds it applies to, operator family). Dynamic names are matched by prefix.
_FIXED_FIELDS = {
    "span.kind": (("any",), "text"),
    "span.name": (("any",), "text"),
    "span.error": (("any", "tool"), "bool"),
    "tool.name": (("tool",), "text"),
    "tool.arguments": (("tool",), "text"),
    "tool.result": (("tool",), "text"),
    "tool.error": (("tool",), "bool"),
    "retrieval.source": (("retrieval_document",), "text"),
    "retrieval.text": (("retrieval_document",), "text"),
    "answer.text": (("answer",), "text"),
}
_DYNAMIC_PREFIXES = {
    "tool.argument.": (("tool",), "any"),
    "tool.result.": (("tool",), "any"),
}


def result_value(step) -> Any:
    raw = step.result if step.result is not None else step.attributes.get("gen_ai.tool.call.result")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _field_spec(field: str) -> tuple[tuple[str, ...], str] | None:
    if field in _FIXED_FIELDS:
        return _FIXED_FIELDS[field]
    for prefix, spec in _DYNAMIC_PREFIXES.items():
        if field.startswith(prefix) and len(field) > len(prefix):
            return spec
    return None


def _validate_ordering(rule: dict) -> list[str]:
    problems: list[str] = []
    check = rule["check"]
    if check == "in_order":
        steps = rule.get("steps")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, list) and s for s in steps):
            problems.append("'in_order' needs a list of steps, each with at least one condition")
        for step in steps or []:
            problems += [p for c in (step if isinstance(step, list) else []) for p in _condition_problems(c)]
        if rule.get("mode", "in_order_others_allowed") not in ("in_order_others_allowed", "in_order_exact"):
            problems.append("'mode' must be in order, others allowed, or in order exactly")
    if check == "before":
        for key in ("first", "then"):
            conditions = rule.get(key)
            if not isinstance(conditions, list) or not conditions:
                problems.append(f"'before' needs '{key}' with at least one condition")
            problems += [p for c in (conditions if isinstance(conditions, list) else []) for p in _condition_problems(c)]
    if check in ("max_group", "max_run"):
        group_by = rule.get("group_by")
        if not isinstance(group_by, list) or not group_by:
            problems.append(f"'{check}' needs 'group_by' with at least one field")
        for field in group_by or []:
            if _field_spec(field) is None:
                problems.append(f"unknown field '{field}'")
        expect = rule.get("expect")
        if not isinstance(expect, dict) or expect.get("op") not in EXPECT_OPS:
            problems.append("'expect' needs an operator: equals, at least, at most, or between")
    return problems


def _condition_problems(condition: Any) -> list[str]:
    if not isinstance(condition, dict) or "field" not in condition or "op" not in condition:
        return ["each condition needs a field and an operator"]
    if _field_spec(condition["field"]) is None:
        return [f"unknown field '{condition['field']}'"]
    return []


def validate_check(rule: dict) -> list[str]:
    """Problems with a custom check rule, in plain words."""
    if rule.get("check") in ORDERING:
        return _validate_ordering(rule)
    problems: list[str] = []
    select = rule.get("select")
    if not isinstance(select, dict) or select.get("kind") not in KINDS:
        problems.append(f"'select.kind' must be one of {', '.join(KINDS)}")
        kind = None
    else:
        kind = select["kind"]
    for index, condition in enumerate(select.get("where", []) if isinstance(select, dict) else []):
        if not isinstance(condition, dict) or "field" not in condition or "op" not in condition:
            problems.append(f"condition {index + 1} needs a field and an operator")
            continue
        spec = _field_spec(condition["field"])
        if spec is None:
            problems.append(f"condition {index + 1}: unknown field '{condition['field']}'")
            continue
        applies, family = spec
        if kind is not None and kind != "any" and kind not in applies and "any" not in applies:
            problems.append(f"condition {index + 1}: '{condition['field']}' does not apply to {kind} units")
        if family == "bool":
            allowed = ("equals", "not_equals")
        elif family == "text":
            allowed = TEXT_OPS + ("at_least", "at_most", "between")
        else:
            allowed = tuple(dict.fromkeys(TEXT_OPS + NUMBER_OPS))
        if condition["op"] not in allowed:
            problems.append(f"condition {index + 1}: operator '{condition['op']}' does not fit '{condition['field']}'")
    check = rule.get("check")
    if check not in CHECKS:
        problems.append(f"'check' must be one of {', '.join(CHECKS)}")
    if check in ("count", "distinct"):
        expect = rule.get("expect")
        if not isinstance(expect, dict) or expect.get("op") not in EXPECT_OPS:
            problems.append("'expect' needs an operator: equals, at least, at most, or between")
        elif expect["op"] == "between" and not (isinstance(expect.get("value"), list) and len(expect["value"]) == 2):
            problems.append("'between' needs two values")
    if check == "distinct" and not rule.get("field"):
        problems.append("'distinct' needs a field to count")
    if rule.get("field") and _field_spec(rule["field"]) is None:
        problems.append(f"unknown field '{rule['field']}'")
    return problems


def _units(kind: str, trajectory: AgentTrajectory) -> list[tuple[Any, str]]:
    """(unit, span_id) pairs for a kind. A unit is a step, a document dict, or the answer text."""
    if kind == "tool":
        return [(s, s.span_id) for s in trajectory.steps if s.kind == "tool"]
    if kind == "action":
        return [(s, s.span_id) for s in trajectory.primary_steps if s.kind in ACTION_KINDS]
    if kind == "retrieval_document":
        out = []
        for step in retrieval_steps(trajectory):
            docs = step.attributes.get("gen_ai.retrieval.documents") or []
            if isinstance(docs, str):
                try:
                    docs = json.loads(docs)
                except json.JSONDecodeError:
                    docs = []
            for doc in docs if isinstance(docs, list) else []:
                if isinstance(doc, dict):
                    out.append((doc, step.span_id))
        return out
    if kind == "answer":
        answers = [s for s in trajectory.primary_steps if s.kind == "inference"]
        return [(final_answer(trajectory), answers[-1].span_id)] if answers else []
    return [(s, s.span_id) for s in trajectory.steps]


def _value(kind: str, unit: Any, field: str) -> Any:
    if kind == "answer":
        return unit if field == "answer.text" else None
    if kind == "retrieval_document":
        if field == "retrieval.source":
            return unit.get("source")
        if field == "retrieval.text":
            return unit.get("passage") or unit.get("content")
        return None
    step = unit
    if field == "span.kind":
        return step.kind
    if field == "span.name":
        return step.name
    if field in ("span.error", "tool.error"):
        return step.is_error()
    if kind == "tool" or step.kind == "tool":
        if field == "tool.name":
            return step.name
        if field == "tool.arguments":
            return json.dumps(tool_arguments(step) or {}, ensure_ascii=False)
        if field == "tool.result":
            value = result_value(step)
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        if field.startswith("tool.argument."):
            return (tool_arguments(step) or {}).get(field[len("tool.argument."):])
        if field.startswith("tool.result."):
            current = result_value(step)
            for part in field[len("tool.result."):].split("."):
                if not isinstance(current, dict) or part not in current:
                    return None
                current = current[part]
            return current
    return None


def _captured(kind: str, unit: Any, field: str) -> bool:
    """Whether the trace recorded the source of a field, even if the key is absent."""
    if field.startswith("tool.argument") or field == "tool.arguments":
        return kind == "tool" and tool_arguments(unit) is not None
    if field.startswith("tool.result") or field == "tool.result":
        return kind == "tool" and result_value(unit) is not None
    return _value(kind, unit, field) is not None


def _compare(value: Any, op: str, expected: Any) -> bool:
    if value is None:
        return False
    if op == "equals":
        return value == expected or str(value) == str(expected)
    if op == "not_equals":
        return not (value == expected or str(value) == str(expected))
    if op == "one_of":
        return str(value) in [str(item) for item in expected]
    if op == "contains":
        return str(expected) in str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if op == "at_least":
        return number >= float(expected)
    if op == "at_most":
        return number <= float(expected)
    if op == "between":
        return float(expected[0]) <= number <= float(expected[1])
    return False


def _matches(kind: str, unit: Any, conditions: list[dict]) -> bool:
    return all(_compare(_value(kind, unit, c["field"]), c["op"], c.get("value")) for c in conditions)


def _expect(n: int, expect: dict) -> bool:
    return _compare(n, expect["op"], expect.get("value"))


def evaluate_check(index: int, rule: dict, trajectory: AgentTrajectory, label: str | None = None) -> CheckResult:
    """One custom check rule as one check result."""
    check_id = label or f"rule.{index}.check"
    if not trajectory.steps:
        return result(check_id, "rules", "INCONCLUSIVE", "No trace evidence captured.")
    if rule.get("check") in ORDERING:
        return _evaluate_ordering(check_id, rule, trajectory)
    select = rule["select"]
    kind = select["kind"]
    conditions = select.get("where", [])
    units = _units(kind, trajectory)
    if not units and kind == "answer":
        return result(check_id, "rules", "INCONCLUSIVE", "The trace has no final answer to check.")
    for condition in conditions:
        if units and not any(_captured(kind, unit, condition["field"]) for unit, _ in units):
            return result(check_id, "rules", "INCONCLUSIVE",
                          f"The trace does not record '{condition['field']}' for {kind} units.")
    matched = [(unit, span) for unit, span in units if _matches(kind, unit, conditions)]
    evidence = [span for _, span in matched if span]
    check = rule["check"]
    if check == "count":
        ok = _expect(len(matched), rule["expect"])
        summary = f"{len(matched)} matching {kind} unit(s); expected {_expect_text(rule['expect'])}."
    elif check == "exists":
        ok = bool(matched)
        summary = f"{len(matched)} matching {kind} unit(s); expected at least 1."
    elif check == "none":
        ok = not matched
        summary = f"{len(matched)} matching {kind} unit(s); expected none."
    elif check == "all":
        ok = len(matched) == len(units)
        summary = f"{len(matched)} of {len(units)} {kind} unit(s) match; expected all."
        evidence = [span for unit, span in units if not _matches(kind, unit, conditions)]
    else:
        values = {str(_value(kind, unit, rule["field"])) for unit, _ in matched}
        values.discard("None")
        ok = _expect(len(values), rule["expect"])
        summary = f"{len(values)} distinct {rule['field']} value(s); expected {_expect_text(rule['expect'])}."
    return result(check_id, "rules", "PASS" if ok else "FAIL", summary,
                  observed=len(matched), evidence_span_ids=evidence)


def _expect_text(expect: dict) -> str:
    names = {"equals": "exactly", "at_least": "at least", "at_most": "at most", "between": "between"}
    value = expect.get("value")
    if expect["op"] == "between":
        return f"between {value[0]} and {value[1]}"
    return f"{names[expect['op']]} {value}"


def _cond(field: str, op: str, value: Any) -> dict:
    return {"field": field, "op": op, "value": value}


def compile_preset(rule: dict) -> list[dict] | None:
    """A preset rule as custom check rules, or None when it stays on its own evaluator
    (ordering presets: sequence, precedes, retries, grounded value, repeated calls)."""
    kind = rule.get("type")
    if kind == "tool_called":
        conditions = [_cond("tool.name", "equals", rule["tool"])]
        conditions += [_cond(f"tool.argument.{k}", "equals", v) for k, v in (rule.get("arguments") or {}).items()]
        low = rule.get("min", 0 if "max" in rule else 1)
        high = rule.get("max")
        if high is None:
            expect = {"op": "at_least", "value": low}
        elif low == 0:
            expect = {"op": "at_most", "value": high}
        else:
            expect = {"op": "between", "value": [low, high]}
        return [{"type": "check", "select": {"kind": "tool", "where": conditions}, "check": "count", "expect": expect}]
    if kind == "tool_not_called":
        conditions = [_cond("tool.name", "equals", rule["tool"])]
        conditions += [_cond(f"tool.argument.{k}", "equals", v) for k, v in (rule.get("arguments") or {}).items()]
        return [{"type": "check", "select": {"kind": "tool", "where": conditions}, "check": "none"}]
    if kind == "budget":
        conditions = [_cond("tool.name", "equals", rule["tool"])] if rule.get("tool") else []
        return [{"type": "check", "select": {"kind": "tool", "where": conditions}, "check": "count",
                 "expect": {"op": "at_most", "value": rule["max"]}}]
    if kind == "errors":
        return [{"type": "check", "select": {"kind": "any", "where": [_cond("span.error", "equals", True)]},
                 "check": "count", "expect": {"op": "at_most", "value": rule["max"]}}]
    if kind == "forbidden_values":
        scopes = rule.get("scope") or ["arguments", "results", "answer"]
        compiled = []
        for value in rule["values"]:
            if "arguments" in scopes:
                compiled.append({"type": "check", "select": {"kind": "tool", "where": [_cond("tool.arguments", "contains", value)]}, "check": "none"})
            if "results" in scopes:
                compiled.append({"type": "check", "select": {"kind": "tool", "where": [_cond("tool.result", "contains", value)]}, "check": "none"})
            if "answer" in scopes:
                compiled.append({"type": "check", "select": {"kind": "answer", "where": [_cond("answer.text", "contains", value)]}, "check": "none"})
        return compiled
    if kind == "sequence" and rule.get("mode", "in_order_others_allowed") in ("in_order_others_allowed", "in_order_exact"):
        return [{"type": "check", "check": "in_order", "steps": [_label_conditions(s) for s in rule["steps"]],
                 "mode": rule.get("mode", "in_order_others_allowed")}]
    if kind == "precedes":
        return [{"type": "check", "check": "before", "first": _label_conditions(rule["first"]),
                 "then": _label_conditions(rule["then"])}]
    if kind == "repeated_calls":
        return [{"type": "check", "check": "max_group", "select": {"kind": "tool", "where": []},
                 "group_by": ["tool.name", "tool.arguments"], "expect": {"op": "at_most", "value": rule["max_identical"]}}]
    if kind == "retrieval":
        compiled = [{"type": "check", "select": {"kind": "retrieval_document", "where": []}, "check": "count",
                     "expect": {"op": "at_least", "value": rule.get("min_documents", 1)}}]
        for source in rule.get("sources", []):
            compiled.append({"type": "check", "select": {"kind": "retrieval_document", "where": [_cond("retrieval.source", "equals", source)]}, "check": "exists"})
        return compiled
    return None


def evaluate_preset(index: int, rule: dict, trajectory: AgentTrajectory) -> CheckResult | None:
    """A compiled preset as ONE check (same id as before), worst status wins."""
    compiled = compile_preset(rule)
    if compiled is None:
        return None
    check_id = f"rule.{index}.{rule['type']}"
    parts = [evaluate_check(index, c, trajectory, check_id) for c in compiled]
    statuses = [p.status for p in parts]
    status = "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    summary = " ".join(dict.fromkeys(p.summary for p in parts))
    evidence = [s for p in parts for s in (p.evidence_span_ids or [])]
    return result(check_id, "rules", status, summary, evidence_span_ids=evidence)


def describe_check(rule: dict) -> str:
    """The custom rule as one sentence."""
    select = rule.get("select", {})
    where = " and ".join(f"{c['field']} {c['op']} {c.get('value', '')}".strip() for c in select.get("where", []))
    scope = f"{select.get('kind', '?')} units" + (f" where {where}" if where else "")
    if rule.get("check") == "none":
        return f"No {scope}"
    if rule.get("check") == "exists":
        return f"At least one {scope}"
    if rule.get("check") == "all":
        return f"All {scope}"
    if rule.get("check") == "distinct":
        return f"Distinct {rule.get('field')} among {scope}: {_expect_text(rule['expect'])}"
    return f"Count of {scope}: {_expect_text(rule['expect'])}"


def _evaluate_ordering(check_id: str, rule: dict, trajectory: AgentTrajectory) -> CheckResult:
    check = rule["check"]
    units = _units("action", trajectory)
    if check == "in_order":
        steps = rule["steps"]
        exact = rule.get("mode") == "in_order_exact"
        position, matched = 0, []
        for unit, span in units:
            if position < len(steps) and _matches("action", unit, steps[position]):
                matched.append(span)
                position += 1
        if exact:
            ok = position == len(steps) and len(units) == len(steps)
        else:
            ok = position == len(steps)
        labels = [_label(u) for u, _ in units]
        summary = f"Actions were {' > '.join(labels) or 'none'}; expected {'exactly ' if exact else ''}in order: {len(steps)} step(s)."
        return result(check_id, "rules", "PASS" if ok else "FAIL", summary, observed=labels,
                      evidence_span_ids=[s for s in matched if s])
    if check == "before":
        first = [i for i, (u, _) in enumerate(units) if _matches("action", u, rule["first"])]
        then = [i for i, (u, _) in enumerate(units) if _matches("action", u, rule["then"])]
        ok = not then or (bool(first) and min(first) < min(then))
        return result(check_id, "rules", "PASS" if ok else "FAIL",
                      "The first step comes before the second." if ok else "The second step occurred without an earlier first step.",
                      observed=[_label(u) for u, _ in units])
    kind = rule.get("select", {}).get("kind", "tool")
    group_by = rule["group_by"]
    selected = [(u, s) for u, s in _units(kind, trajectory) if _matches(kind, u, rule.get("select", {}).get("where", []))]
    if not selected:
        return result(check_id, "rules", "PASS", f"No {kind} units to group.")
    keys = [tuple(str(_value(kind, u, f)) for f in group_by) for u, _ in selected]
    if check == "max_group":
        counts = Counter(keys)
        worst = max(counts.values())
        ok = _expect(worst, rule["expect"])
        return result(check_id, "rules", "PASS" if ok else "FAIL",
                      f"Most identical repeats: {worst}; expected {_expect_text(rule['expect'])}.", observed=worst)
    longest = run = 0
    previous = None
    for key in keys:
        run = run + 1 if key == previous else 1
        longest = max(longest, run)
        previous = key
    ok = _expect(longest, rule["expect"])
    return result(check_id, "rules", "PASS" if ok else "FAIL",
                  f"Longest run of identical calls in a row: {longest}; expected {_expect_text(rule['expect'])}.",
                  observed=longest)


def _label(unit_step) -> str:
    return unit_step.name if unit_step.kind == "tool" else unit_step.kind


def _label_conditions(label: str) -> list[dict]:
    """A step label: a tool name, or a span kind such as retrieval."""
    if label in ACTION_KINDS and label != "tool":
        return [_cond("span.kind", "equals", label)]
    return [_cond("tool.name", "equals", label)]
