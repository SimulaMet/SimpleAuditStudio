"""Agentic scenario schema v2 validation.

The validator intentionally stays dependency-free because scenario metadata is
validated on every write path.  v1 remains readable, while v2 is strict so a
typo cannot silently disable an evaluator.
"""

from copy import deepcopy

_TOP_LEVEL = {
    "schema_version", "trace", "goal", "tools", "retrieval", "trajectory",
    "handoffs", "guardrails", "approvals", "policy", "state", "budgets",
    "semantic_judge", "enforcement", "rerank",
}
_SECTION_KEYS = {
    "trace": {"required", "required_kinds", "content_capture", "missing_evidence"},
    "goal": {"description", "reference_outcome"},
    "tools": {"expected", "allowed", "forbidden", "max_total_calls"},
    "retrieval": {"required", "sources", "forbidden_sources", "min_documents", "max_documents", "content_capture"},
    "trajectory": {"required_sequence", "order_mode", "forbidden_sequences", "max_steps", "max_retries_per_tool", "max_identical_consecutive_calls"},
    "handoffs": {"allowed", "forbidden", "required", "max_handoffs", "max_depth"},
    "guardrails": {"required", "must_pass", "before_actions"},
    "approvals": {"required_for", "must_precede_execution"},
    "policy": {"read_only", "allowed_data_scopes", "forbidden_data_scopes", "forbidden_side_effects", "allowed_destinations", "allow_side_effects"},
    "state": {"assertions"},
    "budgets": {"max_tool_calls", "max_errors", "max_latency_ms", "max_target_tokens", "max_cost_usd"},
    "semantic_judge": {"enabled", "criteria"},
    "enforcement": {"mode", "deterministic_fail_severity", "trace_judge_fail_severity", "missing_required_trace", "severity_overrides"},
    "rerank": {"required", "min_calls", "max_calls"},
}


def validate_agentic_metadata(metadata: dict) -> tuple[bool, list[str]]:
    """Validate agentic metadata schema v2.

    Returns (is_valid, list_of_errors).
    """
    errors = []

    if not isinstance(metadata, dict):
        return False, ["metadata must be a dict"]

    schema_version = metadata.get("schema_version", 1)
    if schema_version == 1:
        # v1 is acceptable but deprecated
        return True, []

    if schema_version != 2:
        errors.append(f"Unsupported schema_version: {schema_version}")
        return False, errors

    unknown = sorted(set(metadata) - _TOP_LEVEL)
    errors.extend(f"Unknown field: {field}" for field in unknown)

    # Required v2 fields
    for field in ["trace", "tools", "retrieval", "trajectory", "enforcement"]:
        if field not in metadata:
            errors.append(f"Missing required field: {field}")

    for section, allowed in _SECTION_KEYS.items():
        value = metadata.get(section)
        if value is not None and isinstance(value, dict):
            errors.extend(
                f"Unknown field: {section}.{field}"
                for field in sorted(set(value) - allowed)
            )

    # Validate trace
    trace = metadata.get("trace", {})
    if not isinstance(trace, dict):
        errors.append("trace must be a dict")
    else:
        if "required" in trace and not isinstance(trace["required"], bool):
            errors.append("trace.required must be boolean")
        if "content_capture" in trace and trace["content_capture"] not in ["optional", "structural", "inputs", "full"]:
            errors.append("trace.content_capture must be optional/structural/inputs/full")

    # Validate tools
    tools = metadata.get("tools", {})
    if not isinstance(tools, dict):
        errors.append("tools must be a dict")
    else:
        if "expected" in tools and not isinstance(tools["expected"], list):
            errors.append("tools.expected must be a list")
        if "forbidden" in tools and not isinstance(tools["forbidden"], list):
            errors.append("tools.forbidden must be a list")

    # Validate enforcement
    enforcement = metadata.get("enforcement", {})
    if not isinstance(enforcement, dict):
        errors.append("enforcement must be a dict")
    else:
        mode = enforcement.get("mode")
        if mode not in ("advisory", "gating"):
            errors.append(f"enforcement.mode must be advisory or gating, got {mode}")

    return len(errors) == 0, errors


def migrate_v1_to_v2(v1_metadata: dict) -> dict:
    """Migrate v1 metadata to v2 format (pure data transform, never mutates v1)."""
    source = deepcopy(v1_metadata)
    v2 = get_schema_v2_template()
    for section, value in source.items():
        if section == "schema_version":
            continue
        if isinstance(value, dict) and isinstance(v2.get(section), dict):
            v2[section].update(value)
        else:
            v2[section] = value
    return v2


def get_schema_v2_template() -> dict:
    """Return empty v2 schema template."""
    return {
        "schema_version": 2,
        "trace": {
            "required": True,
            "required_kinds": [],
            "content_capture": "optional",
            "missing_evidence": "inconclusive",
        },
        "goal": {"description": "", "reference_outcome": None},
        "tools": {
            "expected": [],
            "allowed": [],
            "forbidden": [],
            "max_total_calls": None,
        },
        "retrieval": {
            "required": False,
            "sources": [],
            "forbidden_sources": [],
            "min_documents": None,
            "max_documents": None,
        },
        "trajectory": {
            "required_sequence": [],
            "order_mode": "subsequence",
            "forbidden_sequences": [],
            "max_steps": None,
            "max_retries_per_tool": 1,
            "max_identical_consecutive_calls": 1,
        },
        "handoffs": {
            "allowed": [],
            "forbidden": [],
            "required": [],
            "max_handoffs": 0,
            "max_depth": 0,
        },
        "guardrails": {
            "required": [],
            "must_pass": [],
            "before_actions": [],
        },
        "approvals": {
            "required_for": [],
            "must_precede_execution": True,
        },
        "policy": {
            "read_only": False,
            "allowed_data_scopes": [],
            "forbidden_data_scopes": [],
            "forbidden_side_effects": [],
            "allowed_destinations": [],
            "allow_side_effects": False,
        },
        "rerank": {"required": False, "min_calls": None, "max_calls": None},
        "state": {"assertions": []},
        "budgets": {
            "max_tool_calls": None,
            "max_errors": None,
            "max_latency_ms": None,
            "max_target_tokens": None,
            "max_cost_usd": None,
        },
        "semantic_judge": {
            "enabled": True,
            "criteria": [
                "task_completion",
                "tool_selection",
                "tool_result_handling",
                "grounding",
                "process_safety",
                "efficiency",
            ],
        },
        "enforcement": {
            "mode": "gating",
            "deterministic_fail_severity": "high",
            "trace_judge_fail_severity": "medium",
            "missing_required_trace": "inconclusive",
            "severity_overrides": {},
        },
    }
