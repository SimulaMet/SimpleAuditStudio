from audits.agentic.checks import policy_checks, tool_selection
from audits.agentic.checks.handoffs import handoff_checks
from audits.agentic.checks.trajectory import sequence_checks
from audits.agentic.schema import AgentTrajectory, TrajectoryStep


def step(span, kind, name, **attrs):
    return TrajectoryStep(span, kind, name, index=int(span[1:]), attributes=attrs)


def test_trajectory_order_mode_and_operation_name_are_executable():
    trajectory = AgentTrajectory([
        step("s1", "tool", "lookup"),
        step("s2", "retrieval", "policy"),
    ])
    assert sequence_checks(trajectory, {
        "required_sequence": [{"kind": "tool", "name": "lookup"}, {"kind": "retrieval"}],
        "order_mode": "subsequence",
    })[0].status == "PASS"
    assert sequence_checks(trajectory, {
        "required_sequence": [{"kind": "tool", "name": "wrong"}],
        "order_mode": "exact",
    })[0].status == "FAIL"


def test_tool_allow_list_and_normalized_arguments_are_checked():
    trajectory = AgentTrajectory([
        TrajectoryStep("s1", "tool", "refund", arguments={"order_id": "A-1"}),
    ])
    results = tool_selection(trajectory, {
        "allowed": ["lookup"],
        "expected": [{"name": "refund", "arguments": {"order_id": "A-1"}}],
    })
    assert {item.id: item.status for item in results}["tool.allowed"] == "FAIL"
    assert {item.id: item.status for item in results}["tool.arguments"] == "PASS"


def _arg_status(trajectory, expected_item):
    rs = {item.id: item for item in tool_selection(trajectory, {
        "expected": [expected_item],
    })}
    return rs["tool.arguments"].status


def test_arguments_mode_any_passes_when_one_call_matches():
    trajectory = AgentTrajectory([
        TrajectoryStep("s1", "tool", "lookup",
                       attributes={"gen_ai.tool.call.arguments": '{"order_id": "A-1"}'}),
        TrajectoryStep("s2", "tool", "lookup",
                       attributes={"gen_ai.tool.call.arguments": '{"order_id": "A-2"}'}),
    ])
    # default "all": every call must match
    assert _arg_status(trajectory, {
        "name": "lookup", "arguments": {"order_id": "A-1"},
    }) == "FAIL"
    # "any": at least one call matching is enough (multi-turn probes)
    assert _arg_status(trajectory, {
        "name": "lookup", "arguments": {"order_id": "A-1"},
        "arguments_mode": "any",
    }) == "PASS"
    # "any" still fails when no call matches
    assert _arg_status(trajectory, {
        "name": "lookup", "arguments": {"order_id": "A-9"},
        "arguments_mode": "any",
    }) == "FAIL"


def test_arguments_all_with_uncaptured_call_is_inconclusive():
    trajectory = AgentTrajectory([
        TrajectoryStep("s1", "tool", "lookup",
                       attributes={"gen_ai.tool.call.arguments": '{"order_id": "A-1"}'}),
        TrajectoryStep("s2", "tool", "lookup"),  # arguments not captured
    ])
    assert _arg_status(trajectory, {
        "name": "lookup", "arguments": {"order_id": "A-1"},
    }) == "INCONCLUSIVE"


def test_allowed_destination_without_captured_destination_is_inconclusive():
    result = policy_checks(
        AgentTrajectory([step("s1", "tool", "lookup")]),
        {"allowed_destinations": ["internal.example"]},
    )[0]
    assert result.id == "policy.destination"
    assert result.status == "INCONCLUSIVE"


def test_handoff_destination_and_depth_are_checked():
    trajectory = AgentTrajectory([
        TrajectoryStep("a", "agent", "root"),
        TrajectoryStep("h", "handoff", "handoff", parent_span_id="a"),
    ])
    checks = handoff_checks(trajectory, {"allowed": ["billing"], "max_depth": 0})
    assert {item.id: item.status for item in checks}["handoffs.allowed"] == "INCONCLUSIVE"
    assert {item.id: item.status for item in checks}["handoffs.depth"] == "FAIL"


def test_null_min_calls_does_not_crash():
    trajectory = AgentTrajectory([
        TrajectoryStep("s1", "tool", "lookup",
                       attributes={"gen_ai.tool.call.arguments": '{"order_id": "A-1"}'}),
    ])
    # explicit null min_calls must behave like the default (1)
    results = tool_selection(trajectory, {
        "expected": [{"name": "lookup", "min_calls": None, "max_calls": 1}],
    })
    assert {item.id: item.status for item in results}["tool.required"] == "PASS"

    from audits.agentic.checks import rerank_checks
    assert rerank_checks(AgentTrajectory(), {
        "required": False, "min_calls": None, "max_calls": 1,
    })[0].status == "PASS"


def _no_trajectory():
    return AgentTrajectory([])


def test_optional_retrieval_without_evidence_emits_no_source_check():
    from audits.agentic.checks.retrieval import retrieval_requirements

    results = retrieval_requirements(_no_trajectory(), {
        "required": False,
        "sources": ["Acme Retail Policy"],
    })
    assert [r.id for r in results] == []


def test_required_retrieval_without_evidence_stays_inconclusive():
    from audits.agentic.checks.retrieval import retrieval_requirements

    results = retrieval_requirements(AgentTrajectory([
        step("t1", "inference", "answer"),
    ]), {
        "required": True,
        "sources": ["Acme Retail Policy"],
    })
    statuses = {r.id: r.status for r in results}
    assert statuses["retrieval.required"] == "FAIL"
    assert statuses["retrieval.source_allowed"] == "INCONCLUSIVE"
