"""Handoff and delegation checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def handoff_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    if not expected:
        return []
    handoffs = trajectory.handoffs()
    destinations = [step.handoff_to or step.attributes.get("handoff.to") or step.attributes.get("destination")
                    for step in handoffs]
    out = []
    allowed = set(expected.get("allowed", []))
    forbidden = set(expected.get("forbidden", []))
    if allowed:
        missing = [destination for destination in destinations if not destination]
        bad = [destination for destination in destinations if destination and destination not in allowed]
        status = "INCONCLUSIVE" if missing else ("FAIL" if bad else "PASS")
        out.append(result("handoffs.allowed", "handoffs", status,
                           "All handoff destinations are allowed." if status == "PASS" else "Handoff destination is missing or forbidden.",
                           expected=sorted(allowed), observed=destinations,
                           evidence_span_ids=[s.span_id for s in handoffs if s.span_id]))
    if forbidden:
        bad = [destination for destination in destinations if destination in forbidden]
        out.append(result("handoffs.forbidden", "handoffs", "FAIL" if bad else "PASS",
                           "Forbidden handoff observed." if bad else "No forbidden handoff observed.",
                           expected=sorted(forbidden), observed=bad))
    for required in expected.get("required", []):
        found = required in destinations
        out.append(result("handoffs.required", "handoffs",
                          "PASS" if found else ("INCONCLUSIVE" if not trajectory.steps else "FAIL"),
                          f"Required handoff {required} {'observed' if found else 'not observed'}.",
                          expected=required, observed=destinations))
    if expected.get("max_handoffs") is not None:
        limit = expected["max_handoffs"]
        out.append(result("handoffs.max", "handoffs", "PASS" if len(handoffs) <= limit else "FAIL",
                          f"Observed {len(handoffs)} handoff(s); limit is {limit}.",
                          expected=limit, observed=len(handoffs)))
    if expected.get("max_depth") is not None:
        depths = [len(trajectory.ancestors(step.span_id)) for step in handoffs]
        depth = max(depths, default=0)
        out.append(result("handoffs.depth", "handoffs",
                          "PASS" if depth <= expected["max_depth"] else "FAIL",
                          f"Maximum handoff depth is {depth}; limit is {expected['max_depth']}.",
                          expected=expected["max_depth"], observed=depth))
    return out or [result("handoffs.configured", "handoffs", "PASS", "No handoff constraints configured.")]
