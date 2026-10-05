from audits.agentic.evaluate import evaluate
from audits.agentic.schema import AgentTrajectory, TrajectoryStep


def test_missing_evidence_is_inconclusive_not_pass():
    result = evaluate(AgentTrajectory(), None, {})
    assert result["status"] == "INCONCLUSIVE"
    assert all(c["status"] != "PASS" for c in result["checks"] if c["status"] == "INCONCLUSIVE")


def test_forbidden_side_effect_fails_against_frozen_policy():
    trajectory = AgentTrajectory([TrajectoryStep("s1", "tool", "refund", payload={})])
    result = evaluate(trajectory, None, {"tools": [{"name": "refund", "enabled": True, "read_only": False, "has_side_effects": True}]})
    assert result["status"] == "FAIL"
