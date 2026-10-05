from audits.agentic import normalize


def test_normalizes_standard_and_openinference_shapes_and_preserves_unknown():
    trajectory = normalize([
        {"span_id": "w", "name": "agent workflow", "attributes": {"gen_ai.operation.name": "agent"}},
        {"span_id": "t", "name": "lookup", "attributes": {"openinference.span.kind": "TOOL"}},
        {"span_id": "u", "name": "vendor step", "attributes": {"purpose": "auxiliary"}},
    ])
    assert [step.kind for step in trajectory.steps] == ["workflow", "tool", "unknown"]
    assert len(trajectory.primary_steps) == 2
    assert len(trajectory.auxiliary_steps) == 1
