from audits.agentic.evaluate import evaluate
from audits.agentic.schema import AgentTrajectory, TrajectoryStep


def test_missing_evidence_is_inconclusive_not_pass():
    result = evaluate(AgentTrajectory(), None, {})
    assert result["status"] == "INCONCLUSIVE"
    assert all(c["status"] != "PASS" for c in result["checks"] if c["status"] == "INCONCLUSIVE")


def test_forbidden_side_effect_fails_against_frozen_policy():
    trajectory = AgentTrajectory([TrajectoryStep("s1", "tool", "refund")])
    result = evaluate(trajectory, None, {"tools": [{"name": "refund", "enabled": True, "read_only": False, "has_side_effects": True}]})
    assert result["status"] == "FAIL"


def test_builtin_knowledge_tool_uses_frozen_capability():
    from audits.agentic.checks import tool_permissions

    trajectory = AgentTrajectory([
        TrajectoryStep(
            "s2", "tool", "query_knowledge_files",
            attributes={"gen_ai.tool.type": "builtin"},
        )
    ])

    result = tool_permissions(trajectory, {"capabilities": {"knowledge_search": True}})

    assert result[0].status == "PASS"
    assert result[0].evidence_span_ids == ["s2"]


def test_known_tool_matches_its_frozen_openwebui_invocation_name():
    from audits.agentic.checks import tool_permissions

    trajectory = AgentTrajectory([TrajectoryStep("s1", "tool", "acme_get_order")])
    snapshot = {
        "tools": [{
            "name": "Acme Order Lookup",
            "external_id": "acme_order_lookup",
            "invocation_names": ["acme_get_order"],
            "enabled": True,
            "read_only": True,
            "has_side_effects": False,
        }]
    }

    result = tool_permissions(trajectory, snapshot)

    assert result[0].status == "PASS"
    assert result[0].evidence_span_ids == ["s1"]


