"""Trace integrity checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def trace_integrity(trajectory: AgentTrajectory) -> list[CheckResult]:
    """Check trace presence and error status."""
    if not trajectory.steps:
        return [result("trace.present", "trace", "INCONCLUSIVE", "No trace evidence captured.")]

    errors = [s for s in trajectory.steps if s.is_error()]
    return [
        result(
            "trace.present",
            "trace",
            "PASS",
            "Trace evidence is present.",
            observed=len(trajectory.steps),
        ),
        result(
            "trace.no_error_spans",
            "trace",
            "FAIL" if errors else "PASS",
            "Error spans detected." if errors else "No error spans detected.",
            observed=len(errors),
            evidence_span_ids=[s.span_id for s in errors if s.span_id],
        ),
    ]
