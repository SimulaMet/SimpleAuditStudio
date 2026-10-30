"""Small provider-neutral contract tests for the agentic evaluator (T25)."""

from audits.agentic.checks.base import result
from audits.agentic.trajectory import normalize


def test_trajectory_schema():
    trajectory = normalize([{
        "trace_id": "trace-1",
        "span_id": "span-1",
        "name": "lookup",
        "attributes": {"gen_ai.operation.name": "execute_tool", "tool.name": "lookup"},
    }])
    assert trajectory.steps[0].trace_id == "trace-1"
    assert trajectory.tools()[0].tool_name == "lookup"


def test_check_result_schema():
    check = result(
        "tools.lookup", "tools", "PASS", "Observed", expected={"name": "lookup"},
        observed={"name": "lookup"}, evidence_span_ids=["span-1"],
    )
    assert check.id == "tools.lookup"
    assert check.status == "PASS"
    assert check.evidence_span_ids == ["span-1"]


def test_otel_integration():
    trajectory = normalize([{
        "trace_id": "trace-1", "span_id": "span-1", "name": "tool",
        "attributes": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search"},
    }])
    assert trajectory.tools()[0].tool_name == "search"
