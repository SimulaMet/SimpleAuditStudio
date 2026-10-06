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
