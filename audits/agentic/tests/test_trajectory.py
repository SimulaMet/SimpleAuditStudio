from audits.agentic import normalize


def test_normalizes_genai_execute_tool_name_for_tool_checks():
    trajectory = normalize([
        {
            "span_id": "tool-call",
            "name": "execute_tool",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "acme_lookup_order",
            },
        }
    ])

    assert trajectory.steps[0].kind == "tool"
    assert trajectory.steps[0].name == "acme_lookup_order"


def test_normalizes_standard_and_openinference_shapes_and_preserves_unknown():
    trajectory = normalize([
        {"span_id": "w", "name": "agent workflow", "attributes": {"gen_ai.operation.name": "agent"}},
        {"span_id": "t", "name": "lookup", "attributes": {"openinference.span.kind": "TOOL"}},
        {"span_id": "u", "name": "vendor step", "attributes": {"purpose": "auxiliary"}},
    ])
    assert [step.kind for step in trajectory.steps] == ["workflow", "tool", "unknown"]
    assert len(trajectory.primary_steps) == 2
    assert len(trajectory.auxiliary_steps) == 1
