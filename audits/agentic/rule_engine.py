"""One rule engine: select trace units, then check them.

A rule reads as one sentence:
  look at <kind> units where <conditions>, check <count | exists | none | all | distinct>,
  expect <comparison>.

Units are read only from the trace: tool calls (name, arguments, result, error),
retrieval documents (source, text), the final answer, and any step. Missing evidence
is INCONCLUSIVE. Rules are ANDed: each one is checked on its own.
"""
from __future__ import annotations

import json
from typing import Any

from .checks.answer import final_answer
from .checks.base import CheckResult, result
from .checks.retrieval import _retrieval_steps as retrieval_steps
from .checks.tools import _tool_arguments as tool_arguments
from .schema import AgentTrajectory

KINDS = ("tool", "retrieval_document", "answer", "any")
CHECKS = ("count", "exists", "none", "all", "distinct")
TEXT_OPS = ("equals", "not_equals", "one_of", "not_one_of", "contains")
NUMBER_OPS = ("equals", "at_least", "at_most", "between")
EXPECT_OPS = ("equals", "at_least", "at_most", "between")
_RULE_KEYS = {"type", "check", "select", "expect", "field"}

# Field name -> (kinds it applies to, operator family). Dynamic names are matched by prefix.
_FIXED_FIELDS = {
    "span.kind": (("any",), "text"),
    "span.name": (("any",), "text"),
    "span.error": (("any", "tool"), "bool"),
    "span.error_type": (("any", "tool"), "text"),
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


def _condition_problems(condition: Any) -> list[str]:
    if not isinstance(condition, dict) or "field" not in condition or "op" not in condition:
        return ["each condition needs a field and an operator"]
    if _field_spec(condition["field"]) is None:
        return [f"unknown field '{condition['field']}'"]
    return []


def _count_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _expect_problems(expect: Any) -> list[str]:
    if not isinstance(expect, dict) or expect.get("op") not in EXPECT_OPS:
        return ["'expect' needs an operator: equals, at least, at most, or between"]
    value = expect.get("value")
    if expect["op"] == "between":
        ok = isinstance(value, list) and len(value) == 2 and all(_count_ok(v) for v in value) and value[0] <= value[1]
        return [] if ok else ["'between' needs two counts, low first"]
    return [] if _count_ok(value) else ["'expect' needs a count of 0 or more"]


def validate_check(rule: dict) -> list[str]:
    """Problems with a rule, in plain words."""
    problems: list[str] = []
    unknown = set(rule) - _RULE_KEYS
    if unknown:
        problems.append(f"unknown field(s): {', '.join(sorted(unknown))}")
    check = rule.get("check")
    if check not in CHECKS:
        return problems + [f"'check' must be one of {', '.join(CHECKS)}"]
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
    if check in ("count", "distinct"):
        problems += _expect_problems(rule.get("expect"))
    if check == "distinct" and not rule.get("field"):
        problems.append("'distinct' needs a field to count")
    if rule.get("field") and _field_spec(rule["field"]) is None:
        problems.append(f"unknown field '{rule['field']}'")
    return problems


def _units(kind: str, trajectory: AgentTrajectory) -> list[tuple[Any, str]]:
    """(unit, span_id) pairs for a kind. A unit is a step, a document dict, or the answer text."""
    if kind == "tool":
        return [(s, s.span_id) for s in trajectory.steps if s.kind == "tool"]
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
    if field == "span.error_type":
        return step.attributes.get("error.type")
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
    if field == "span.error_type" and hasattr(unit, "is_error") and not unit.is_error():
        # An error type exists only on failed steps: a step that did not fail has none to record.
        return True
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
    if op == "not_one_of":
        return str(value) not in [str(item) for item in expected]
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
    """One rule as one check result."""
    check_id = label or f"rule.{index}.check"
    if rule.get("check") not in CHECKS:
        return result(check_id, "rules", "INCONCLUSIVE", unsupported_text(rule))
    if not trajectory.steps:
        return result(check_id, "rules", "INCONCLUSIVE", "No trace evidence captured.")
    select = rule["select"]
    kind = select["kind"]
    conditions = select.get("where", [])
    units = _units(kind, trajectory)
    if not units and kind == "answer":
        return result(check_id, "rules", "INCONCLUSIVE", "The trace has no final answer to check.")
    if not units and rule["check"] == "all":
        return result(check_id, "rules", "INCONCLUSIVE", f"No {kind} units to check, so 'all' cannot be shown.")
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


_FIELD_NAMES = {
    "span.kind": "span kind", "span.name": "span name", "span.error": "error",
    "span.error_type": "error type", "tool.name": "tool name", "tool.arguments": "arguments",
    "tool.result": "result", "tool.error": "tool error", "retrieval.source": "source",
    "retrieval.text": "retrieved text", "answer.text": "answer text",
}
_KIND_NAMES = {"tool": "tool call", "retrieval_document": "retrieved document", "answer": "answer", "any": "step"}
_OP_WORDS = {"equals": "is", "not_equals": "is not", "one_of": "is one of", "not_one_of": "is not one of",
             "contains": "contains", "at_least": "is at least", "at_most": "is at most", "between": "is between"}
_EXPECT_WORDS = {"equals": "Exactly", "at_least": "At least", "at_most": "At most", "between": "Between"}


def _field_name(field: str) -> str:
    if field.startswith("tool.argument."):
        return f"argument {field[len('tool.argument.'):]}"
    if field.startswith("tool.result."):
        return f"result {field[len('tool.result.'):]}"
    return _FIELD_NAMES.get(field, field)


def _condition_text(condition: dict) -> str:
    field, op, value = condition["field"], condition["op"], condition.get("value")
    if field in ("span.error", "tool.error") and op == "equals" and value is True:
        return "is an error"
    if isinstance(value, bool):
        value = "yes" if value else "no"
    elif isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return f"{_field_name(field)} {_OP_WORDS[op]} {value}"


def _conditions_text(where: list[dict]) -> str:
    return " and ".join(_condition_text(c) for c in where)


def _scope_text(select: dict) -> str:
    kind = _KIND_NAMES.get(select.get("kind"), select.get("kind") or "step")
    where = list(select.get("where", []))
    errors = [c for c in where if c["field"] == "span.error" and c["op"] == "equals" and c.get("value") is True]
    if errors:
        where = [c for c in where if c not in errors]
        kind = "error" if kind == "step" else f"error {kind}"
    return f"{kind} where {_conditions_text(where)}" if where else kind


def _expect_phrase(expect: dict) -> str:
    value = expect.get("value")
    if expect["op"] == "between":
        return f"Between {value[0]} and {value[1]}"
    return f"{_EXPECT_WORDS[expect['op']]} {value}"


def _is_tool_name(condition: dict) -> bool:
    return condition["field"] == "tool.name" and condition["op"] == "equals"


def _tool_call_sentence(check: str, rule: dict, where: list[dict]) -> str:
    """'name(arg=value) must be called at least 2 times' for tool-name counts."""
    name = where[0]["value"]
    args = [c for c in where[1:] if c["field"].startswith("tool.argument.") and c["op"] == "equals"]
    if len(args) != len(where) - 1:
        return f"{_expect_phrase(rule['expect'])} tool call where {_conditions_text(where)}"
    target = name
    if args:
        target += "(" + ", ".join(f"{c['field'][len('tool.argument.'):]}={c['value']!r}" for c in args) + ")"
    if check == "none":
        return f"{target} must not be called"
    expect = rule["expect"]
    value = expect.get("value")
    if expect["op"] in ("equals", "at_most") and value == 0:
        return f"{target} must not be called"
    if expect["op"] == "between":
        return f"{target} must be called {value[0]} to {value[1]} times"
    if expect["op"] == "equals" and value == 1:
        return f"{target} must be called exactly once"
    if expect["op"] == "at_least" and value == 1:
        return f"{target} must be called at least once"
    word = {"equals": "exactly", "at_least": "at least", "at_most": "at most"}[expect["op"]]
    return f"{target} must be called {word} {value} times"


def unsupported_text(rule: dict) -> str:
    """The message for a rule the engine can no longer evaluate: what it was and what to do."""
    legacy = rule.get("legacy") if isinstance(rule.get("legacy"), dict) else {}
    name = legacy.get("type") or rule.get("check") or rule.get("type") or "rule"
    return f"No longer supported ({name}): edit this rule to a count, exists or none check."


def describe_check(rule: dict) -> str:
    """The rule as one readable sentence."""
    check = rule.get("check")
    if check not in CHECKS:
        return unsupported_text(rule)
    select = rule.get("select", {})
    where = select.get("where", [])
    if check in ("count", "none") and select.get("kind") == "tool" and where and _is_tool_name(where[0]):
        return _tool_call_sentence(check, rule, where)
    if check in ("count", "none") and select.get("kind") == "any" and where == [
            {"field": "span.error", "op": "equals", "value": True}]:
        if check == "none" or rule["expect"]["op"] == "at_most" and rule["expect"]["value"] == 0:
            return "No error steps"
        return f"{_expect_phrase(rule['expect'])} error step{'s' if rule['expect']['value'] != 1 else ''}"
    scope = _scope_text(select)
    if check == "none":
        return f"No {scope} occurs"
    if check == "exists":
        return f"At least one {scope} occurs"
    if check == "all":
        return f"Every {scope} must match"
    if check == "distinct":
        return f"{_expect_phrase(rule['expect'])} distinct {_field_name(rule.get('field', ''))} among {scope}"
    return f"{_expect_phrase(rule['expect'])} {scope}"


def field_catalog() -> list[dict]:
    """Fields a rule can read, with the units they apply to and their type."""
    fixed = [{"name": name, "kinds": list(kinds), "family": family} for name, (kinds, family) in _FIXED_FIELDS.items()]
    dynamic = [{"name": prefix, "kinds": list(kinds), "family": family, "needs_key": True}
               for prefix, (kinds, family) in _DYNAMIC_PREFIXES.items()]
    return fixed + dynamic


_CODE_OPS = {"equals": "==", "not_equals": "!=", "one_of": "in", "not_one_of": "not in",
             "contains": "contains", "at_least": ">=", "at_most": "<="}


def _code_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_code_value(v) for v in value) + "]"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _code_condition(condition: dict) -> str:
    op, value = condition["op"], condition.get("value")
    if op == "between":
        return f"{condition['field']} between {_code_value(value[0])} and {_code_value(value[1])}"
    return f"{condition['field']} {_CODE_OPS[op]} {_code_value(value)}"


def _code_expect(expect: dict) -> str:
    value = expect.get("value")
    if expect["op"] == "between":
        return f"between {_code_value(value[0])} and {_code_value(value[1])}"
    return f"{_CODE_OPS.get(expect['op'], expect['op'])} {_code_value(value)}"


def _code_conditions(conditions: list[dict]) -> str:
    return " and ".join(_code_condition(c) for c in conditions) or "true"


def check_code(rule: dict) -> str:
    """A rule as read-only code. Presentation only: it is not parsed back."""
    check = rule.get("check")
    if check not in CHECKS:
        return unsupported_text(rule)
    select = rule["select"]
    where = f" where {_code_conditions(select['where'])}" if select.get("where") else ""
    if check == "distinct":
        return f"distinct {rule.get('field')} of {select['kind']}{where} count {_code_expect(rule['expect'])}"
    if check == "count":
        return f"count {select['kind']}{where} {_code_expect(rule['expect'])}"
    return f"{check} {select['kind']}{where}"
