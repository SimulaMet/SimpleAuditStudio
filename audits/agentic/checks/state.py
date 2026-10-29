"""State assertion and side-effect checks."""
from ..schema import AgentTrajectory
from .base import CheckResult, result


def state_checks(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    if not expected or not expected.get("assertions"):
        return []
    state_steps = trajectory.find(kind="state")
    if not state_steps:
        return [result("state.evidence", "state", "INCONCLUSIVE", "State assertions have no captured state evidence.")]
    before = state_steps[0].attributes.get("before", {})
    after = state_steps[-1].attributes.get("after", {})
    out = []
    for assertion in expected["assertions"]:
        kind, key = assertion.get("type"), assertion.get("key")
        if kind == "exists":
            ok = key in after
        elif kind == "changed":
            ok = before.get(key) != after.get(key)
        elif kind == "equals":
            ok = after.get(key) == assertion.get("value")
        elif kind == "no_mutation":
            ok = before.get(key) == after.get(key)
        else:
            out.append(result("state.assertion", "state", "ERROR", f"Unknown state assertion type: {kind}"))
            continue
        out.append(result(f"state.{kind}_{key}", "state", "PASS" if ok else "FAIL",
                          f"State assertion {kind} for {key}: {ok}.",
                          expected=assertion, observed={"before": before.get(key), "after": after.get(key)},
                          evidence_span_ids=[step.span_id for step in state_steps if step.span_id]))
    return out
