"""Resource and budget checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result

def budget_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    return [result("budgets.steps", "budgets", "PASS", "Budget check placeholder.")]
