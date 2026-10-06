"""Trajectory and sequence checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def sequence_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    """Check trajectory sequences against expectations."""
    out = []

    if required_sequence := expected.get("required_sequence"):
        out.extend(_check_exact_sequence(trajectory, required_sequence))

    if required_subsequence := expected.get("required_subsequence"):
        out.extend(_check_subsequence(trajectory, required_subsequence))

    if order_constraints := expected.get("partial_order"):
        out.extend(_check_partial_order(trajectory, order_constraints))

    if forbidden := expected.get("forbidden_sequences"):
        out.extend(_check_forbidden_sequences(trajectory, forbidden))

    if retry_config := expected.get("loop_detection"):
        out.extend(_check_loops(trajectory, retry_config))

    return out or [result("trajectory.sequence", "trajectory", "PASS", "No trajectory constraints.")]


def _check_exact_sequence(trajectory: AgentTrajectory, required: list[dict]) -> list[CheckResult]:
    """Exact sequence: only these operations in exact order."""
    steps = trajectory.primary_steps
    required_kinds = [r.get("kind") for r in required]
    actual_kinds = [s.kind for s in steps]

    if actual_kinds == required_kinds:
        return [result("trajectory.exact_sequence", "trajectory", "PASS", f"Exact sequence matched: {actual_kinds}")]

    return [
        result(
            "trajectory.exact_sequence",
            "trajectory",
            "FAIL",
            f"Exact sequence mismatch. Expected {required_kinds}, got {actual_kinds}",
            expected=required_kinds,
            observed=actual_kinds,
        )
    ]


def _check_subsequence(trajectory: AgentTrajectory, required: list[dict]) -> list[CheckResult]:
    """Subsequence: required operations in order but may have other ops between."""
    steps = trajectory.primary_steps
    actual_kinds = [s.kind for s in steps]
    required_kinds = [r.get("kind") for r in required]

    idx = 0
    for req_kind in required_kinds:
        try:
            idx = actual_kinds.index(req_kind, idx) + 1
        except ValueError:
            return [
                result(
                    "trajectory.subsequence",
                    "trajectory",
                    "FAIL",
                    f"Required operation {req_kind} not found in sequence",
                    expected=required_kinds,
                    observed=actual_kinds,
                )
            ]

    return [result("trajectory.subsequence", "trajectory", "PASS", f"Subsequence found: {required_kinds}")]


def _check_partial_order(trajectory: AgentTrajectory, constraints: list[dict]) -> list[CheckResult]:
    """Partial order: A must come before B."""
    out = []
    steps = trajectory.primary_steps

    for constraint in constraints:
        before_kind = constraint.get("before")
        after_kind = constraint.get("after")

        before_idx = next((i for i, s in enumerate(steps) if s.kind == before_kind), -1)
        after_idx = next((i for i, s in enumerate(steps) if s.kind == after_kind), -1)

        if before_idx == -1 or after_idx == -1:
            status = "INCONCLUSIVE"
            summary = f"Missing operation: {before_kind if before_idx == -1 else after_kind}"
        elif before_idx < after_idx:
            status = "PASS"
            summary = f"{before_kind} correctly before {after_kind}"
        else:
            status = "FAIL"
            summary = f"{before_kind} must come before {after_kind}, but didn't"

        out.append(
            result(
                f"trajectory.order_{before_kind}_{after_kind}",
                "trajectory",
                status,
                summary,
                expected=constraint,
            )
        )

    return out


def _check_forbidden_sequences(trajectory: AgentTrajectory, forbidden: list[list[str]]) -> list[CheckResult]:
    """Forbidden sequences: detect and fail if present."""
    out = []
    steps = trajectory.primary_steps
    actual_kinds = [s.kind for s in steps]

    for forbidden_seq in forbidden:
        if _contains_sequence(actual_kinds, forbidden_seq):
            out.append(
                result(
                    f"trajectory.forbidden_{' '.join(forbidden_seq)}",
                    "trajectory",
                    "FAIL",
                    f"Forbidden sequence detected: {' -> '.join(forbidden_seq)}",
                    observed=actual_kinds,
                )
            )

    return out or [result("trajectory.forbidden_sequences", "trajectory", "PASS", "No forbidden sequences found.")]


def _check_loops(trajectory: AgentTrajectory, config: dict) -> list[CheckResult]:
    """Loop/retry detection: identical calls, budget exceeded, repeated failures."""
    out = []
    steps = trajectory.steps

    max_retries = config.get("max_retries_per_tool", 1)
    max_identical = config.get("max_identical_consecutive", 1)
    max_steps = config.get("max_steps")

    if max_steps and len(steps) > max_steps:
        out.append(
            result(
                "trajectory.step_budget",
                "trajectory",
                "FAIL",
                f"Step budget exceeded: {len(steps)} > {max_steps}",
                observed=len(steps),
            )
        )

    tool_calls = {}
    consecutive_identical = 1
    prev_tool = None

    for step in steps:
        if step.kind == "tool":
            key = (step.tool_name, str(step.arguments))
            tool_calls[key] = tool_calls.get(key, 0) + 1

            if prev_tool == key:
                consecutive_identical += 1
            else:
                if consecutive_identical > max_identical:
                    out.append(
                        result(
                            "trajectory.identical_calls",
                            "trajectory",
                            "FAIL",
                            f"Identical consecutive calls exceeded: {consecutive_identical} > {max_identical}",
                            observed=consecutive_identical,
                        )
                    )
                consecutive_identical = 1
                prev_tool = key

    for (tool_name, args), count in tool_calls.items():
        if count > max_retries:
            out.append(
                result(
                    f"trajectory.retry_{tool_name}",
                    "trajectory",
                    "FAIL",
                    f"Tool {tool_name} called {count} times (max {max_retries})",
                    observed=count,
                )
            )

    return out or [result("trajectory.loops", "trajectory", "PASS", "No loop/retry issues detected.")]


def _contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    """Check if needle sequence appears in haystack."""
    if not needle:
        return False
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i : i + len(needle)] == needle:
            return True
    return False
