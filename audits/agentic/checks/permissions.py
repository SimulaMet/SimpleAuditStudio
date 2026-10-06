"""Permission and side-effect checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def permission_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    return [result("permissions.policy", "permissions", "PASS", "Permission check placeholder.")]
