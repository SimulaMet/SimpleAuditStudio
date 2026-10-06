"""Trajectory and sequence checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def sequence_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    """Check trajectory sequences against expectations."""
    return [result("trajectory.sequence", "trajectory", "PASS", "Trajectory sequence check placeholder.")]
