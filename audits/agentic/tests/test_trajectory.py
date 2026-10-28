from audits.agentic import normalize


def test_normalizes_genai_execute_tool_name_for_tool_checks():
    trajectory = normalize([
        {
            "span_id": "tool-call",
            "name": "execute_tool",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "acme_get_order",
            },
        }
    ])

    assert trajectory.steps[0].kind == "tool"
    assert trajectory.steps[0].name == "acme_get_order"


def test_normalizes_standard_and_openinference_shapes_and_preserves_unknown():
    trajectory = normalize([
        {"span_id": "w", "name": "agent workflow", "attributes": {"gen_ai.operation.name": "agent"}},
        {"span_id": "t", "name": "lookup", "attributes": {"openinference.span.kind": "TOOL"}},
        {"span_id": "u", "name": "vendor step", "attributes": {"purpose": "auxiliary"}},
    ])
    assert [step.kind for step in trajectory.steps] == ["workflow", "tool", "unknown"]
    assert len(trajectory.primary_steps) == 2
    assert len(trajectory.auxiliary_steps) == 1


def test_synthesizes_data_scope_from_order_id_argument():
    trajectory = normalize([
        {
            "span_id": "t",
            "name": "execute_tool",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "acme_get_order",
                "gen_ai.tool.call.arguments": '{"order_id": "ACME-1001"}',
            },
        }
    ])
    step = trajectory.steps[0]
    assert step.attributes["data_scope"] == ["order:ACME-1001"]


def test_data_scope_synthesis_leaves_non_order_arguments_alone():
    trajectory = normalize([
        {
            "span_id": "t",
            "name": "execute_tool",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "query_knowledge_bases",
                "gen_ai.tool.call.arguments": '{"query": "refund policy"}',
            },
        }
    ])
    assert trajectory.steps[0].attributes.get("data_scope") is None


def test_explicit_data_scope_attribute_wins_over_synthesis():
    trajectory = normalize([
        {
            "span_id": "t",
            "name": "execute_tool",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "acme_get_order",
                "gen_ai.tool.call.arguments": '{"order_id": "ACME-1001"}',
                "data_scope": ["order:ACME-9999"],
            },
        }
    ])
    assert trajectory.steps[0].attributes["data_scope"] == ["order:ACME-9999"]
