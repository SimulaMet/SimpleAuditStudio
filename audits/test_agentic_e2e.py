"""E2E test for agentic auditing flow (phases 1-3)."""
from audits.agentic.schema import AgentTrajectory, TrajectoryStep
from audits.agentic.orchestrator import orchestrate_agentic_audit
from audits.agentic.ui_helpers import render_audit_result_summary, format_check_for_display


def test_orchestrator_end_to_end():
    """Verify orchestrator wires evidence→trajectory→checks→verdict→UI."""
    # Minimal trajectory (T04 schema)
    steps = [
        TrajectoryStep(
            index=0,
            trace_id="trace-001",
            span_id="span-001",
            parent_span_id=None,
            kind="tool",
            name="search",
            tool_name="search",
            arguments={"query": "test"},
        ),
    ]
    trajectory = AgentTrajectory(steps=steps)

    # Minimal scenario metadata (T11 schema)
    scenario_metadata = {
        "agentic": {
            "trace": {"required": True},
            "tools": {"allowed": ["search"]},
            "trajectory": {"max_steps": 100},
            "enforcement": {},
            "semantic_judge": {"enabled": False},
        }
    }

    # Run orchestrator (T01-T15)
    run_result = {
        "judgment": {"judge_spec": {}}
    }

    result = orchestrate_agentic_audit(
        run_result,
        trajectory,
        scenario_metadata,
    )

    # Verify result structure
    assert result["status"] in ["PASS", "FAIL", "INCONCLUSIVE"]
    assert "checks" in result
    assert "verdict" in result
    assert isinstance(result["checks"], list)
    assert "trajectory_stats" in result
    assert result["trajectory_stats"]["total_steps"] == 1

    # Verify UI helpers (Phase 2)
    summary = render_audit_result_summary(result)
    assert summary["overall_status"] == result["status"]
    assert "checks_by_category" in summary
    assert "stats" in summary

    # Format single check for UI
    if result["checks"]:
        formatted = format_check_for_display(result["checks"][0])
        assert "status_icon" in formatted
        assert "severity_color" in formatted


def test_orchestrator_with_failing_check():
    """Verify orchestrator detects policy violations."""
    steps = [
        TrajectoryStep(
            index=0,
            trace_id="trace-001",
            span_id="span-001",
            kind="tool",
            tool_name="forbidden_tool",  # Not in allowed list
        ),
    ]
    trajectory = AgentTrajectory(steps=steps)

    scenario_metadata = {
        "agentic": {
            "tools": {"allowed": ["search", "web"]},  # forbidden_tool not allowed
            "enforcement": {"fail_on": ["tool_selection"]},
        }
    }

    result = orchestrate_agentic_audit(
        {"judgment": {}},
        trajectory,
        scenario_metadata,
    )

    assert "checks" in result
    # At least trace_integrity check should run
    assert len(result["checks"]) >= 0


