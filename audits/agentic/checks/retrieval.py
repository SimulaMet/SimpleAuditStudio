"""Retrieval and source checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def retrieval_requirements(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    """Check retrieval against expected requirements."""
    retrievals = trajectory.retrievals()
    required = expected.get("required", False)

    if required and not retrievals:
        return [result("retrieval.required", "retrieval", "FAIL", "Retrieval required but not performed.")]

    return [result("retrieval.required", "retrieval", "PASS", f"Retrieval check: {len(retrievals)} retrieval(s) found.")]
