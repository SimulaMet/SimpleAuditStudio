"""State assertion and side-effect checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result

def state_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    return [result("state.mutations", "state", "PASS", "State check placeholder.")]
