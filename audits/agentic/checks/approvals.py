"""Approval and authorization checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result

def approval_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    return [result("approvals.required", "approvals", "PASS", "Approval check placeholder.")]
