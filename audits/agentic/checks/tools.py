"""Tool selection and usage checks."""
import json
from typing import Any

from ..schema import AgentTrajectory
from .base import CheckResult, result


def _tool_arguments(step: Any) -> dict | None:
    """Extract tool arguments from a step's attributes."""
    raw = step.attributes.get("gen_ai.tool.call.arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _arguments_match(observed: Any, expected: Any) -> bool:
    """Check if observed arguments match expected (subset matching)."""
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and _arguments_match(observed[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(observed, list) and len(observed) >= len(expected) and all(
            _arguments_match(actual, value) for actual, value in zip(observed, expected)
        )
    return observed == expected


def tool_selection(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    """Check tool calls against expected requirements."""
    calls = [s for s in trajectory.primary_steps if s.kind == "tool"]
    names = [s.name for s in calls]
    required = expected.get("expected", [])
    out = []

    for item in required:
        name = item.get("name")
        matching = [step for step in calls if step.name == name]
        count = len(matching)
        lo, hi = item.get("min_calls", 1), item.get("max_calls")
        ok = count >= lo and (hi is None or count <= hi)

        out.append(
            result(
                "tool.required",
                "tool",
                "PASS" if ok else "FAIL",
                f"Required tool {name}: {count} call(s).",
                expected=item,
                observed=count,
                evidence_span_ids=[step.span_id for step in matching if step.span_id],
            )
        )

        expected_arguments = item.get("arguments")
        if expected_arguments is not None:
            observed_arguments = [_tool_arguments(step) for step in matching]
            missing_capture = not matching or any(value is None for value in observed_arguments)
            argument_status = (
                "INCONCLUSIVE"
                if missing_capture
                else "PASS"
                if all(_arguments_match(value, expected_arguments) for value in observed_arguments)
                else "FAIL"
            )

            out.append(
                result(
                    "tool.arguments",
                    "tool",
                    argument_status,
                    f"Arguments for {name} are {'unavailable' if missing_capture else 'checked'}.",
                    expected=expected_arguments,
                    observed=None if missing_capture else observed_arguments,
                    evidence_span_ids=[step.span_id for step in matching if step.span_id],
                )
            )

    forbidden = set(expected.get("forbidden", []))
    if forbidden:
        bad = forbidden.intersection(names)
        out.append(
            result(
                "tool.forbidden",
                "tool",
                "FAIL" if bad else "PASS",
                "Forbidden tool called." if bad else "No forbidden tools called.",
                observed=sorted(bad),
            )
        )

    return out


def tool_permissions(trajectory: AgentTrajectory, snapshot: dict) -> list[CheckResult]:
    """Check tool permissions against frozen agent snapshot."""
    snapshot = snapshot or {}
    tools = {}

    for tool in snapshot.get("tools", []):
        if not isinstance(tool, dict):
            continue
        aliases = [tool.get("name"), tool.get("external_id"), *(tool.get("invocation_names") or [])]
        for alias in aliases:
            if alias:
                tools[str(alias)] = tool

    results = []

    for step in [s for s in trajectory.steps if s.kind == "tool"]:
        tool = tools.get(step.name)
        evidence = [step.span_id] if step.span_id else []

        if str(step.attributes.get("gen_ai.tool.type", "")).lower() == "builtin":
            capability = _builtin_capability(step.name)
            enabled = (snapshot.get("capabilities") or {}).get(capability) if capability else None

            if capability and enabled is not None:
                results.append(
                    result(
                        "tool.builtin_allowed",
                        "permission",
                        "PASS" if enabled else "FAIL",
                        f"Builtin {step.name} maps to frozen capability {capability}.",
                        observed=enabled,
                        evidence_span_ids=evidence,
                    )
                )
                continue

        if tool is None:
            results.append(
                result(
                    "tool.known",
                    "permission",
                    "INCONCLUSIVE",
                    f"Tool {step.name} is not in frozen policy.",
                    evidence_span_ids=evidence,
                )
            )
        else:
            allowed = tool.get("enabled", True) and (
                tool.get("read_only", True) or not tool.get("has_side_effects", False)
            )
            results.append(
                result(
                    "tool.side_effect_allowed",
                    "permission",
                    "PASS" if allowed else "FAIL",
                    f"Policy for {step.name}.",
                    observed=tool,
                    evidence_span_ids=evidence,
                )
            )

    return results or [result("tool.known", "permission", "INCONCLUSIVE", "No tool evidence captured.")]


def _builtin_capability(tool_name: str) -> str | None:
    """Map builtin tool names to capabilities."""
    if tool_name in {"list_knowledge", "search_knowledge_files", "query_knowledge_files", "grep_knowledge_files"}:
        return "knowledge_search"
    return None
