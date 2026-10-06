"""Guardrail and policy checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result

def guardrail_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    return [result("guardrails.required", "guardrails", "PASS", "Guardrail check placeholder.")]
