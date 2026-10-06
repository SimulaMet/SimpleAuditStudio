from audits.agentic.evaluate import evaluate
from audits.agentic.schema import AgentTrajectory, TrajectoryStep


def test_retrieval_check_uses_knowledge_tool_and_missing_source_is_inconclusive():
    from audits.agentic.expectations import validate_agentic_metadata

    trajectory = AgentTrajectory([
        TrajectoryStep(
            "r1", "tool", "query_knowledge_files",
            attributes={"gen_ai.tool.type": "builtin"},
        )
    ])
    expectations = validate_agentic_metadata({
        "agentic": {
            "schema_version": 1,
            "retrieval": {"required": True, "sources": ["Acme Retail Policy"]},
        }
    })

    result = evaluate(
        trajectory, expectations,
        {"capabilities": {"knowledge_search": True}},
    )
    checks = {check["id"]: check for check in result["checks"]}

    assert checks.get("retrieval.required") is not None
    assert checks["retrieval.required"]["status"] == "PASS"
    assert checks["retrieval.source_allowed"]["status"] == "INCONCLUSIVE"


def test_retrieval_source_resolves_frozen_knowledge_base_id():
    from audits.agentic.expectations import validate_agentic_metadata

    expectations = validate_agentic_metadata({
        "agentic": {
            "schema_version": 1,
            "retrieval": {"required": True, "sources": ["Acme Retail Policy"]},
        }
    })
    trajectory = AgentTrajectory([
        TrajectoryStep(
            "r2", "retrieval", "retrieval",
            attributes={"openwebui.retrieval.data_source": ["kb-acme"]},
        )
    ])
    result = evaluate(trajectory, expectations, {
        "knowledge_bases": [{"name": "Acme Retail Policy", "external_id": "kb-acme"}]
    })
    source_check = next(
        check for check in result["checks"] if check["id"] == "retrieval.source_allowed"
    )

    assert source_check["status"] == "PASS"
    assert source_check["evidence_span_ids"] == ["r2"]


def test_retrieval_source_resolves_search_result_file_to_attached_knowledge_base():
    import json

    from audits.agentic.expectations import validate_agentic_metadata

    expectations = validate_agentic_metadata({
        "agentic": {
            "schema_version": 1,
            "retrieval": {"required": True, "sources": ["Acme Retail Policy"]},
        }
    })
    trajectory = AgentTrajectory([
        TrajectoryStep(
            "list", "tool", "list_knowledge",
            attributes={
                "gen_ai.tool.call.result": json.dumps({
                    "knowledge_bases": [{
                        "id": "kb-acme",
                        "name": "Acme Retail Policy",
                        "files": [{"id": "file-1", "filename": "refunds_and_returns_policy.md"}],
                    }],
                }),
            },
        ),
        TrajectoryStep(
            "search", "tool", "query_knowledge_files",
            attributes={
                "gen_ai.tool.call.result": json.dumps([{
                    "content": "x" * 1500,
                    "source": "refunds_and_returns_policy.md",
                    "file_id": "file-1",
                    "trailing": "x" * 2000,
                }])[:2000],
            },
        ),
    ])

    result = evaluate(trajectory, expectations, {
        "knowledge_bases": [{"name": "Acme Retail Policy", "external_id": "kb-acme"}]
    })
    source_check = next(
        check for check in result["checks"] if check["id"] == "retrieval.source_allowed"
    )

    assert source_check["status"] == "PASS"
    assert source_check["observed"] == ["Acme Retail Policy"]
    assert source_check["evidence_span_ids"] == ["search"]


def test_retrieval_source_uses_single_attached_kb_for_truncated_search_result():
    import json

    from audits.agentic.expectations import validate_agentic_metadata

    expectations = validate_agentic_metadata({
        "agentic": {
            "schema_version": 1,
            "retrieval": {"required": True, "sources": ["Acme Retail Policy"]},
        }
    })
    result_text = json.dumps([{
        "content": "x" * 1500,
        "source": "refunds_and_returns_policy.md",
        "file_id": "file-1",
        "trailing": "x" * 2000,
    }])[:2000]
    trajectory = AgentTrajectory([
        TrajectoryStep(
            "search", "tool", "query_knowledge_files",
            attributes={"gen_ai.tool.call.result": result_text},
        )
    ])

    result = evaluate(trajectory, expectations, {
        "knowledge_bases": [{"name": "Acme Retail Policy", "external_id": "kb-acme"}]
    })
    source_check = next(
        check for check in result["checks"] if check["id"] == "retrieval.source_allowed"
    )

    assert source_check["status"] == "PASS"
    assert source_check["observed"] == ["Acme Retail Policy"]


def test_required_retrieval_fails_when_trace_has_no_retrieval_operation():
    from audits.agentic.expectations import validate_agentic_metadata

    expectations = validate_agentic_metadata({
        "agentic": {"schema_version": 1, "retrieval": {"required": True}}
    })
    result = evaluate(
        AgentTrajectory([TrajectoryStep("i1", "inference", "chat_completion")]),
        expectations,
        {},
    )
    retrieval = next(
        (check for check in result["checks"] if check["id"] == "retrieval.required"),
        None,
    )

    assert retrieval is not None
    assert retrieval["status"] == "FAIL"


def test_no_tool_expectations_do_not_make_successful_retrieval_inconclusive():
    from audits.agentic.expectations import validate_agentic_metadata

    expectations = validate_agentic_metadata({
        "agentic": {
            "schema_version": 1,
            "tools": {"expected": [], "forbidden": []},
            "retrieval": {"required": True},
        }
    })
    trajectory = AgentTrajectory([
        TrajectoryStep(
            "search", "tool", "query_knowledge_files",
            attributes={"gen_ai.tool.type": "builtin"},
        )
    ])

    result = evaluate(trajectory, expectations, {
        "capabilities": {"knowledge_search": True}
    })

    assert result["status"] == "PASS"
    assert all(check["id"] != "tool.required" for check in result["checks"])


def test_missing_evidence_is_inconclusive_not_pass():
    result = evaluate(AgentTrajectory(), None, {})
    assert result["status"] == "INCONCLUSIVE"
    assert all(c["status"] != "PASS" for c in result["checks"] if c["status"] == "INCONCLUSIVE")


def test_tool_argument_assertion_uses_captured_standard_genai_arguments():
    from audits.agentic.checks import tool_selection

    trajectory = AgentTrajectory([
        TrajectoryStep(
            "s3", "tool", "acme_lookup_order",
            attributes={"gen_ai.tool.call.arguments": '{"order_id":"ACME-1001"}'},
        )
    ])

    results = tool_selection(trajectory, {
        "expected": [{
            "name": "acme_lookup_order",
            "min_calls": 1,
            "max_calls": 1,
            "arguments": {"order_id": "ACME-1001"},
        }],
        "forbidden": [],
    })

    assert [(result.id, result.status) for result in results] == [
        ("tool.required", "PASS"),
        ("tool.arguments", "PASS"),
    ]
    assert results[1].evidence_span_ids == ["s3"]


def test_forbidden_side_effect_fails_against_frozen_policy():
    trajectory = AgentTrajectory([TrajectoryStep("s1", "tool", "refund", payload={})])
    result = evaluate(trajectory, None, {"tools": [{"name": "refund", "enabled": True, "read_only": False, "has_side_effects": True}]})
    assert result["status"] == "FAIL"


def test_builtin_knowledge_tool_uses_frozen_capability():
    from audits.agentic.checks import tool_permissions

    trajectory = AgentTrajectory([
        TrajectoryStep(
            "s2", "tool", "query_knowledge_files",
            attributes={"gen_ai.tool.type": "builtin"},
        )
    ])

    result = tool_permissions(trajectory, {"capabilities": {"knowledge_search": True}})

    assert result[0].status == "PASS"
    assert result[0].evidence_span_ids == ["s2"]


def test_known_tool_matches_its_frozen_openwebui_invocation_name():
    from audits.agentic.checks import tool_permissions

    trajectory = AgentTrajectory([TrajectoryStep("s1", "tool", "acme_lookup_order")])
    snapshot = {
        "tools": [{
            "name": "Acme Order Lookup",
            "external_id": "acme_order_lookup",
            "invocation_names": ["acme_lookup_order"],
            "enabled": True,
            "read_only": True,
            "has_side_effects": False,
        }]
    }

    result = tool_permissions(trajectory, snapshot)

    assert result[0].status == "PASS"
    assert result[0].evidence_span_ids == ["s1"]
