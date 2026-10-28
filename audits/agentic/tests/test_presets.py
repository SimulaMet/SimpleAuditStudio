"""Preset tests: minimal sections, no dormant defaults, adders chain."""
import pytest

from audits.agentic.presets import (
    PRESETS,
    add_approval_required,
    add_budget,
    add_handoff_required,
    add_policy_data_scopes,
    add_policy_read_only,
    add_retrieval_required,
    add_semantic_judge,
    add_tool_expected,
    add_tool_forbidden,
    base_metadata,
    build_preset,
)
from audits.agentic.schema_v2 import validate_agentic_metadata


def _ok(metadata):
    valid, errors = validate_agentic_metadata(metadata)
    assert valid, errors
    return metadata


def test_base_is_sparse_and_valid():
    m = _ok(base_metadata())
    assert set(m) == {"schema_version", "trace", "enforcement"}


def test_preset_tool_accuracy_emits_only_tools():
    m = _ok(build_preset("tool_accuracy", order_id="ACME-1001"))
    assert set(m) == {"schema_version", "trace", "enforcement", "tools"}
    assert m["tools"]["expected"] == [{
        "name": "acme_get_order", "min_calls": 1,
        "arguments": {"order_id": "ACME-1001"}, "arguments_mode": "any",
    }]
    # no retrieval/trajectory/policy/budgets silently filled
    for section in ("retrieval", "trajectory", "policy", "budgets", "semantic_judge"):
        assert section not in m


def test_preset_retrieval_provenance_emits_only_retrieval():
    m = _ok(build_preset("retrieval_provenance", source="Acme Retail Policy"))
    assert set(m) == {"schema_version", "trace", "enforcement", "retrieval"}
    assert m["retrieval"] == {"required": True, "sources": ["Acme Retail Policy"], "min_documents": 1}


def test_preset_forbidden_actions():
    m = _ok(build_preset("forbidden_actions", "acme_create_refund_request", read_only=True))
    assert set(m) == {"schema_version", "trace", "enforcement", "tools", "policy"}
    assert m["tools"]["forbidden"] == ["acme_create_refund_request"]
    assert m["policy"] == {"read_only": True}


def test_preset_semantic_grounding():
    m = _ok(build_preset("semantic_grounding"))
    assert set(m) == {"schema_version", "trace", "enforcement", "semantic_judge"}
    assert m["semantic_judge"] == {
        "enabled": True, "criteria": ["tool_result_handling", "grounding"],
    }


def test_preset_recovery_budget():
    m = _ok(build_preset("recovery_budget", "acme_get_tracking_events", max_calls=2, max_tool_calls=3))
    assert m["tools"]["expected"][0] == {
        "name": "acme_get_tracking_events", "min_calls": 1, "max_calls": 2,
    }
    assert m["budgets"] == {"max_tool_calls": 3}


def test_build_preset_unknown_name_raises():
    with pytest.raises(ValueError, match="Unknown preset"):
        build_preset("vibes")
    assert sorted(PRESETS) == [
        "forbidden_actions", "recovery_budget", "retrieval_provenance",
        "semantic_grounding", "tool_accuracy",
    ]


def test_adders_are_chainable_and_minimal():
    m = base_metadata()
    assert add_tool_expected(m, "acme_get_order", arguments={"order_id": "X"}) is m
    add_tool_forbidden(m, "acme_send_customer_email")
    add_retrieval_required(m, "Acme Retail Policy", min_documents=1)
    add_policy_read_only(m)
    add_policy_data_scopes(m, "customer:CUST-001")
    add_approval_required(m, "acme_create_refund_request")
    add_handoff_required(m, "Returns Specialist")
    add_budget(m, max_tool_calls=5)
    add_semantic_judge(m, "process_safety", "grounding")
    _ok(m)
    assert m["approvals"]["must_precede_execution"] is True
    assert m["handoffs"] == {"max_handoffs": 1, "required": ["Returns Specialist"]}


def test_adder_does_not_duplicate_entries():
    m = base_metadata()
    add_tool_forbidden(m, "a")
    add_tool_forbidden(m, "a", "b")
    add_retrieval_required(m, "kb")
    add_retrieval_required(m, "kb")
    assert m["tools"]["forbidden"] == ["a", "b"]
    assert m["retrieval"]["sources"] == ["kb"]


def test_unknown_semantic_criteria_rejected():
    with pytest.raises(ValueError, match="criteria"):
        add_semantic_judge(base_metadata(), "not_a_criterion")
