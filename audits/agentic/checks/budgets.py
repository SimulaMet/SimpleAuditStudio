"""Resource and budget checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def _configured(expected: dict, key: str) -> bool:
    return expected.get(key) is not None


def _metric(step, *keys):
    for key in keys:
        value = step.attributes.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def budget_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    if not expected:
        return []

    checks = []
    tools = trajectory.tools()
    errors = trajectory.errors()

    limits = {
        "max_tool_calls": (len(tools), "budgets.tool_calls", "tool calls"),
        "max_errors": (len(errors), "budgets.errors", "errors"),
        "max_handoffs": (len(trajectory.handoffs()), "budgets.handoffs", "handoffs"),
    }
    for key, (observed, check_id, label) in limits.items():
        if _configured(expected, key):
            limit = expected[key]
            checks.append(result(
                check_id, "budgets", "PASS" if observed <= limit else "FAIL",
                f"Observed {observed} {label}; limit is {limit}.",
                expected=limit, observed=observed,
            ))

    metric_specs = {
        "max_latency_ms": ("budgets.latency", "latency", ("duration_ms",)),
        "max_target_tokens": (
            "budgets.target_tokens", "target tokens",
            ("gen_ai.usage.total_tokens", "gen_ai.usage.output_tokens", "target_tokens"),
        ),
        "max_cost_usd": (
            "budgets.cost", "cost", ("gen_ai.usage.cost", "cost_usd", "gen_ai.cost_usd"),
        ),
    }
    for key, (check_id, label, keys) in metric_specs.items():
        if not _configured(expected, key):
            continue
        values = [step.duration_ms if keys == ("duration_ms",) else _metric(step, *keys)
                  for step in trajectory.steps]
        if any(value is None for value in values):
            checks.append(result(
                check_id, "budgets", "INCONCLUSIVE",
                f"{label.title()} evidence is incomplete.", expected=expected[key],
            ))
            continue
        observed = sum(values)
        limit = expected[key]
        checks.append(result(
            check_id, "budgets", "PASS" if observed <= limit else "FAIL",
            f"Observed {observed:g} {label}; limit is {limit}.",
            expected=limit, observed=observed,
        ))

    return checks or [result("budgets.configured", "budgets", "PASS", "No budgets configured.")]
