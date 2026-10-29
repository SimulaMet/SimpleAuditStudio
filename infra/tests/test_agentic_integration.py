def test_agentic_evaluator_failure_payload_is_contained():
    from audits.agentic.evaluate import evaluate
    from audits.agentic.schema import AgentTrajectory

    result = evaluate(AgentTrajectory(), None, {})
    assert result["status"] == "INCONCLUSIVE"
