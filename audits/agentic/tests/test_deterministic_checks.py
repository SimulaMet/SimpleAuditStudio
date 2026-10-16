from audits.agentic.evaluate import evaluate
from audits.agentic.schema import AgentTrajectory, TrajectoryStep


def _step(span, kind, name, **attributes):
    return TrajectoryStep(span, kind, name, index=int(span[1:]) if span[1:].isdigit() else 0,
                          attributes=attributes)


def test_remaining_deterministic_checks_are_wired_and_fail_real_violations():
    trajectory = AgentTrajectory([
        _step("s1", "tool", "refund", has_side_effects=True, data_scope="order:other"),
        _step("s2", "approval", "approval", decision="rejected", for_action="refund"),
    ])
    metadata = {"agentic": {
        "schema_version": 1,
        "rerank": {"required": True},
        "policy": {"read_only": True, "allowed_data_scopes": ["order:ACME-1001"]},
        "budgets": {"max_tool_calls": 0},
        "approvals": {"required_for": ["refund"]},
        "guardrails": {"required": ["privacy"]},
        "handoffs": {"max_handoffs": 0},
        "state": {"assertions": [{"type": "equals", "key": "status", "value": "refunded"}]},
    }}
    result = evaluate(trajectory, metadata, {})
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert statuses["rerank.required"] == "FAIL"
    assert statuses["policy.read_only"] == "FAIL"
    assert statuses["policy.data_scope"] == "FAIL"
    assert statuses["budgets.tool_calls"] == "FAIL"
    assert statuses["approvals.required"] == "FAIL"
    assert statuses["guardrails.required"] == "FAIL"
    assert statuses["state.evidence"] == "INCONCLUSIVE"


def test_deterministic_checks_pass_with_observed_evidence():
    trajectory = AgentTrajectory([
        _step("s1", "guardrail", "privacy", decision="passed"),
        _step("s2", "approval", "approval", decision="approved", for_action="refund"),
        _step("s3", "rerank", "reranker"),
        _step("s4", "tool", "refund", has_side_effects=False, data_scope="order:ACME-1001"),
        _step("s5", "state", "state", before={"status": "pending"}, after={"status": "refunded"}),
    ])
    result = evaluate(trajectory, {"agentic": {
        "schema_version": 1,
        "rerank": {"required": True},
        "policy": {"read_only": False, "allowed_data_scopes": ["order:ACME-1001"]},
        "budgets": {"max_tool_calls": 1, "max_errors": 0},
        "guardrails": {"required": ["privacy"], "must_pass": ["privacy"],
                        "before_actions": [{"guardrail": "privacy", "action": "refund"}]},
        "approvals": {"required_for": ["refund"]},
        "state": {"assertions": [{"type": "equals", "key": "status", "value": "refunded"}]},
    }}, {})
    assert result["status"] == "PASS"
    assert all(check["status"] == "PASS" for check in result["checks"])


def _trajectory(name, tool_type="builtin"):
    from audits.agentic import normalize

    return normalize([
        {
            "span_id": "s1",
            "name": "execute_tool",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.type": tool_type,
            },
        }
    ])


def test_unmapped_builtin_tool_is_out_of_scope_for_frozen_policy():
    from audits.agentic.checks.tools import tool_permissions

    results = tool_permissions(_trajectory("search_chats"), {"capabilities": {"memory": False}})
    # Runtime-granted builtin: not a frozen-policy violation, not evidence either
    # (the trailing "no tool evidence" filler must not mask the real step).
    assert results == []


def test_mapped_builtin_tool_still_checked_against_capability():
    from audits.agentic.checks.tools import tool_permissions

    results = tool_permissions(_trajectory("search_knowledge_bases"), {"capabilities": {"knowledge_search": True}})
    assert {r.id for r in results} == {"tool.builtin_allowed"}
    assert results[0].status == "PASS"


def test_unknown_non_builtin_tool_remains_inconclusive():
    from audits.agentic.checks.tools import tool_permissions

    results = tool_permissions(_trajectory("rogue_tool", tool_type="function"), {"tools": []})
    assert {r.id for r in results} == {"tool.known"}
    assert results[0].status == "INCONCLUSIVE"


def _builtin_step(span, name):
    return TrajectoryStep(span, "tool", name, index=int(span[1:]), attributes={
        "gen_ai.tool.type": "builtin",
    })


def test_runtime_builtin_not_counted_as_domain_tool_in_allowlist():
    # get_current_timestamp is a runtime builtin: it must not trip tool.allowed
    # even when the allow-list only covers domain tools.
    from audits.agentic.checks.tools import tool_selection

    trajectory = AgentTrajectory([
        _step("s1", "tool", "lookup"),
        _builtin_step("s2", "get_current_timestamp"),
    ])
    statuses = {r.id: r.status for r in tool_selection(trajectory, {
        "allowed": ["lookup"],
    })}
    assert statuses["tool.allowed"] == "PASS"


def test_runtime_builtin_not_counted_against_expected_domain_tools():
    # A builtin call is not evidence of (or against) expected domain tools.
    from audits.agentic.checks.tools import tool_selection

    trajectory = AgentTrajectory([_builtin_step("s1", "search_chats")])
    statuses = {r.id: r.status for r in tool_selection(trajectory, {
        "expected": [{"name": "lookup"}],
    })}
    assert statuses["tool.required"] == "FAIL"


def test_runtime_builtin_named_in_forbidden_list_still_fails():
    from audits.agentic.checks.tools import tool_selection

    trajectory = AgentTrajectory([
        _builtin_step("s1", "search_chats"),
        _step("s2", "tool", "lookup"),
    ])
    statuses = {r.id: r.status for r in tool_selection(trajectory, {
        "forbidden": ["search_chats"],
    })}
    assert statuses["tool.forbidden"] == "FAIL"


def test_runtime_builtin_without_type_attribute_is_still_excluded_from_allowlist():
    # Even if the trace omits gen_ai.tool.type, known OWUI runtime builtins
    # are recognized by name and excluded from domain-tool allowlists.
    from audits.agentic.checks.tools import tool_selection

    trajectory = AgentTrajectory([
        _step("s1", "tool", "get_current_timestamp"),
        _step("s2", "tool", "lookup"),
    ])
    statuses = {r.id: r.status for r in tool_selection(trajectory, {
        "allowed": ["lookup"],
    })}
    assert statuses["tool.allowed"] == "PASS"
