"""Trace integrity: presence only. Error handling is a per-scenario rule, not a default check."""
from audits.agentic.checks.trace import trace_integrity
from audits.agentic.trajectory import normalize


def _span(status):
    return {"span_id": "s1", "name": "execute_tool", "status": status, "attributes": {
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "lookup"}}


def test_error_spans_do_not_fail_the_trace_by_default():
    checks = trace_integrity(normalize([_span("ERROR")]))
    assert [c.id for c in checks] == ["trace.present"]
    assert checks[0].status == "PASS"


def test_empty_trace_is_inconclusive():
    checks = trace_integrity(normalize([]))
    assert [(c.id, c.status) for c in checks] == [("trace.present", "INCONCLUSIVE")]
