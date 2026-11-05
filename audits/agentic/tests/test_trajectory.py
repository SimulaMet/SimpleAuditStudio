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


def test_normalizes_genai_and_preserves_unknown():
    trajectory = normalize([
        {"span_id": "w", "name": "agent workflow", "attributes": {"gen_ai.operation.name": "invoke_agent"}},
        {"span_id": "t", "name": "lookup", "attributes": {"gen_ai.operation.name": "execute_tool"}},
        {"span_id": "u", "name": "vendor step", "attributes": {"purpose": "auxiliary"}},
    ])
    assert [step.kind for step in trajectory.steps] == ["workflow", "tool", "unknown"]
    assert len(trajectory.primary_steps) == 2
    assert len(trajectory.auxiliary_steps) == 1


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


def test_failed_order_lookup_discloses_no_scope():
    trajectory = normalize([
        {
            "span_id": "t",
            "name": "execute_tool",
            "status": "ERROR",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "acme_get_order",
                "gen_ai.tool.call.arguments": '{"order_id": "ACME-9999"}',
            },
        }
    ])
    assert trajectory.steps[0].attributes.get("data_scope") is None

def test_a_model_call_inside_a_retrieval_span_is_not_an_answer():
    """Query rewriting nested in a retrieval span (OTel GenAI) is a retrieval step, not the reply."""
    import json as _json

    from audits.agentic.checks.answer import final_answer
    from audits.agentic.trajectory import normalize

    def out(text):
        return _json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": text}]}])

    spans = [
        {"span_id": "w", "name": "invoke_workflow rag", "attributes": {"gen_ai.operation.name": "invoke_workflow"}},
        {"span_id": "r", "parent_span_id": "w", "name": "retrieval",
         "attributes": {"gen_ai.operation.name": "retrieval", "gen_ai.retrieval.query.text": "change GP"}},
        {"span_id": "q", "parent_span_id": "r", "name": "chat default",
         "attributes": {"gen_ai.operation.name": "chat", "gen_ai.output.messages": out("change GP")}},
        {"span_id": "a", "parent_span_id": "w", "name": "chat default",
         "attributes": {"gen_ai.operation.name": "chat", "gen_ai.output.messages": out("Log in to helsenorge.no.")}},
    ]
    trajectory = normalize(spans)
    purposes = {step.span_id: step.purpose for step in trajectory.steps}
    assert purposes["q"] == "auxiliary"
    assert purposes["a"] == "primary"
    # Even when the rewrite is the last model call, it is never taken as the answer.
    assert "helsenorge" in final_answer(normalize([spans[0], spans[3], spans[1], spans[2]]))
