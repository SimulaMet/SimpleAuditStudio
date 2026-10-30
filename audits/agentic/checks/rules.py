"""Rule checks: every rule is a custom check, evaluated from trace spans only."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def rule_checks(trajectory: AgentTrajectory, rules: list[dict]) -> list[CheckResult]:
    """One check per rule. Every rule is a custom check."""
    from ..rule_engine import evaluate_check
    from ..rules import describe_rule

    out = []
    for index, rule in enumerate(rules):
        if rule.get("type") != "check":
            out.append(result(f"rule.{index}.check", "rules", "INCONCLUSIVE", "Not a custom check; edit this rule."))
            continue
        if not trajectory.steps:
            check = result(f"rule.{index}.check", "rules", "INCONCLUSIVE", "No trace evidence captured.")
        else:
            check = evaluate_check(index, rule, trajectory)
        check.details = {**check.details, "rule": describe_rule(rule)}
        out.append(check)
    return out
