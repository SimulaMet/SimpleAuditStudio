"""Pure deterministic agentic checks."""
from dataclasses import dataclass, field
from typing import Any

from .schema import AgentTrajectory


@dataclass
class CheckResult:
    id: str
    category: str
    status: str
    severity: str = ""
    summary: str = ""
    expected: Any = None
    observed: Any = None
    evidence_span_ids: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _result(check_id, category, status, summary, **kwargs):
    return CheckResult(check_id, category, status, summary=summary, **kwargs)


def trace_integrity(trajectory: AgentTrajectory) -> list[CheckResult]:
    if not trajectory.steps:
        return [_result("trace.present", "trace", "INCONCLUSIVE", "No trace evidence captured.")]
    errors = [s for s in trajectory.steps if str(s.status).lower() in {"error", "failed"}]
    return [_result("trace.present", "trace", "PASS", "Trace evidence is present.",
                    observed=len(trajectory.steps)),
            _result("trace.no_error_spans", "trace", "FAIL" if errors else "PASS",
                    "Error spans detected." if errors else "No error spans detected.",
                    observed=len(errors), evidence_span_ids=[s.span_id for s in errors if s.span_id])]


def tool_selection(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    calls = [s for s in trajectory.primary_steps if s.kind == "tool"]
    names = [s.name for s in calls]
    required = expected.get("expected", [])
    out = []
    for item in required:
        name = item.get("name")
        count = names.count(name)
        lo, hi = item.get("min_calls", 1), item.get("max_calls")
        ok = count >= lo and (hi is None or count <= hi)
        out.append(_result("tool.required", "tool", "PASS" if ok else "FAIL",
                           f"Required tool {name}: {count} call(s).", expected=item, observed=count))
    forbidden = set(expected.get("forbidden", []))
    if forbidden:
        bad = forbidden.intersection(names)
        out.append(_result("tool.forbidden", "tool", "FAIL" if bad else "PASS",
                           "Forbidden tool called." if bad else "No forbidden tools called.", observed=sorted(bad)))
    return out or [_result("tool.required", "tool", "INCONCLUSIVE", "No tool expectation or tool evidence.")]


def tool_permissions(trajectory: AgentTrajectory, snapshot: dict) -> list[CheckResult]:
    tools = {str(t.get("name")): t for t in (snapshot or {}).get("tools", []) if isinstance(t, dict)}
    results = []
    for step in [s for s in trajectory.steps if s.kind == "tool"]:
        tool = tools.get(step.name)
        if tool is None:
            results.append(_result("tool.known", "permission", "INCONCLUSIVE", f"Tool {step.name} is not in frozen policy."))
        else:
            allowed = tool.get("enabled", True) and (tool.get("read_only", True) or not tool.get("has_side_effects", False))
            results.append(_result("tool.side_effect_allowed", "permission", "PASS" if allowed else "FAIL", f"Policy for {step.name}.", observed=tool))
    return results or [_result("tool.known", "permission", "INCONCLUSIVE", "No tool evidence captured.")]


def evaluate_checks(trajectory, expectations, snapshot):
    results = trace_integrity(trajectory)
    tools = (expectations.tools if expectations else {})
    results += tool_selection(trajectory, tools)
    results += tool_permissions(trajectory, snapshot)
    return results
