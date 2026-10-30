"""Trace integrity checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def trace_integrity(trajectory: AgentTrajectory) -> list[CheckResult]:
    """Check that trace evidence exists. Error handling is set per scenario by rules."""
    if not trajectory.steps:
        return [result("trace.present", "trace", "INCONCLUSIVE", "No trace evidence captured.")]

    return [
        result(
            "trace.present",
            "trace",
            "PASS",
            "Trace evidence is present.",
            observed=len(trajectory.steps),
        ),
    ]
