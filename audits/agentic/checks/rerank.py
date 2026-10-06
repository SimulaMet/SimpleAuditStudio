"""Reranking evidence and result-count checks."""

from ..schema import AgentTrajectory
from .base import CheckResult, result


def rerank_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    """Check required reranking operations and optional count bounds."""
    if not expected:
        return []
    steps = trajectory.find(kind="rerank")
    checks = []
    if expected.get("required"):
        status = "PASS" if steps else ("INCONCLUSIVE" if not trajectory.steps else "FAIL")
        checks.append(result(
            "rerank.required", "rerank", status,
            "Rerank evidence observed." if steps else "Required rerank evidence is absent.",
            evidence_span_ids=[step.span_id for step in steps if step.span_id],
        ))
    if expected.get("min_calls") is not None or expected.get("max_calls") is not None:
        low, high = expected.get("min_calls", 0), expected.get("max_calls")
        ok = len(steps) >= low and (high is None or len(steps) <= high)
        checks.append(result(
            "rerank.count", "rerank", "PASS" if ok else "FAIL",
            f"Observed {len(steps)} rerank operation(s).",
            expected={"min": low, "max": high}, observed=len(steps),
            evidence_span_ids=[step.span_id for step in steps if step.span_id],
        ))
    return checks or [result("rerank.configured", "rerank", "PASS", "No rerank constraints configured.")]
