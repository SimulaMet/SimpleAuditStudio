"""Agentic scenario schema v2 validation.

The validator intentionally stays dependency-free because scenario metadata is
validated on every write path.  v1 remains readable, while v2 is strict so a
typo cannot silently disable an evaluator.

Sparse semantics (v2):
- Every section except ``schema_version`` is optional.  An *absent* section is
  simply not checked by the evaluators; it must never be filled with silent
  defaults that turn absence into an assertion.
- A field present with an explicit value (including ``false``, ``0`` or
  ``null``) IS an assertion and is validated strictly.
- ``trace.missing_evidence`` keeps its "inconclusive" semantics: when evidence
  is missing, affected checks are reported as INCONCLUSIVE rather than FAIL.
"""

from copy import deepcopy

_TOP_LEVEL = {
    "schema_version", "trace", "goal", "tools", "retrieval", "trajectory",
    "handoffs", "guardrails", "approvals", "policy", "state", "budgets",
    "semantic_judge", "enforcement", "rerank", "rules",
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

_TRACE_CONTENT_CAPTURES = ("optional", "structural", "inputs", "full")
_TRACE_MISSING_EVIDENCE = ("inconclusive", "fail")
_ENFORCEMENT_MODES = ("advisory", "gating")
_SEVERITIES = ("low", "medium", "high", "critical")
_TRAJECTORY_ORDER_MODES = ("subsequence", "exact")

# section.key -> (kind, allowed_values_or_None)
# kind: "bool" | "int" (non-negative) | "float" (non-negative) | "str" |
#       "list" | "dict" | "choices"
_FIELD_SPECS = {
    # trace
    ("trace", "required"): ("bool", None),
    ("trace", "required_kinds"): ("list", None),
    ("trace", "content_capture"): ("choices", _TRACE_CONTENT_CAPTURES),
    ("trace", "missing_evidence"): ("choices", _TRACE_MISSING_EVIDENCE),
    # goal
    ("goal", "description"): ("str", None),
    ("goal", "reference_outcome"): ("nullable_str", None),
    # tools
    ("tools", "expected"): ("list", None),
    ("tools", "allowed"): ("list", None),
    ("tools", "forbidden"): ("list", None),
    ("tools", "max_total_calls"): ("nullable_int", None),
    # retrieval
    ("retrieval", "required"): ("bool", None),
    ("retrieval", "sources"): ("list", None),
    ("retrieval", "forbidden_sources"): ("list", None),
    ("retrieval", "min_documents"): ("nullable_int", None),
    ("retrieval", "max_documents"): ("nullable_int", None),
    # content_capture is a boolean on retrieval (privacy gating of captured
    # retrieved text); the string levels live on trace.content_capture.
    ("retrieval", "content_capture"): ("bool", None),
    # trajectory
    ("trajectory", "required_sequence"): ("list", None),
    ("trajectory", "order_mode"): ("choices", _TRAJECTORY_ORDER_MODES),
    ("trajectory", "forbidden_sequences"): ("list", None),
    ("trajectory", "max_steps"): ("nullable_int", None),
    ("trajectory", "max_retries_per_tool"): ("nullable_int", None),
    ("trajectory", "max_identical_consecutive_calls"): ("nullable_int", None),
    # handoffs
    ("handoffs", "allowed"): ("list", None),
    ("handoffs", "forbidden"): ("list", None),
    ("handoffs", "required"): ("list", None),
    ("handoffs", "max_handoffs"): ("nullable_int", None),
    ("handoffs", "max_depth"): ("nullable_int", None),
    # guardrails
    ("guardrails", "required"): ("list", None),
    ("guardrails", "must_pass"): ("list", None),
    ("guardrails", "before_actions"): ("list", None),
    # approvals
    ("approvals", "required_for"): ("list", None),
    ("approvals", "must_precede_execution"): ("bool", None),
    # policy
    ("policy", "read_only"): ("bool", None),
    ("policy", "allowed_data_scopes"): ("list", None),
    ("policy", "forbidden_data_scopes"): ("list", None),
    ("policy", "forbidden_side_effects"): ("list", None),
    ("policy", "allowed_destinations"): ("list", None),
    ("policy", "allow_side_effects"): ("bool", None),
    # state
    ("state", "assertions"): ("list", None),
    # budgets
    ("budgets", "max_tool_calls"): ("nullable_int", None),
    ("budgets", "max_errors"): ("nullable_int", None),
    ("budgets", "max_latency_ms"): ("nullable_int", None),
    ("budgets", "max_target_tokens"): ("nullable_int", None),
    ("budgets", "max_cost_usd"): ("nullable_float", None),
    # semantic_judge
    ("semantic_judge", "enabled"): ("bool", None),
    ("semantic_judge", "criteria"): ("list", None),
    # enforcement
    ("enforcement", "mode"): ("choices", _ENFORCEMENT_MODES),
    ("enforcement", "deterministic_fail_severity"): ("choices", _SEVERITIES),
    ("enforcement", "trace_judge_fail_severity"): ("choices", _SEVERITIES),
    ("enforcement", "missing_required_trace"): ("choices", _TRACE_MISSING_EVIDENCE),
    ("enforcement", "severity_overrides"): ("dict", None),
    # rerank
    ("rerank", "required"): ("bool", None),
    ("rerank", "min_calls"): ("nullable_int", None),
    ("rerank", "max_calls"): ("nullable_int", None),
}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_field(section: str, key: str, value, errors: list[str]) -> None:
    """Strictly type-check one supplied field of a section."""
    spec = _FIELD_SPECS.get((section, key))
    if spec is None:
        return  # unknown keys are reported separately
    kind, choices = spec
    where = f"{section}.{key}"
    if kind == "bool":
        if not isinstance(value, bool):
            errors.append(f"{where} must be boolean")
    elif kind == "str":
        if not isinstance(value, str):
            errors.append(f"{where} must be a string")
    elif kind == "nullable_str":
        if value is not None and not isinstance(value, str):
            errors.append(f"{where} must be a string or null")
    elif kind == "int":
        if not _is_int(value) or value < 0:
            errors.append(f"{where} must be a non-negative integer")
    elif kind in ("nullable_int", "nullable_float"):
        if value is not None:
            ok = (_is_int(value) and value >= 0) if kind == "nullable_int" \
                else (isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0)
            if not ok:
                errors.append(f"{where} must be a non-negative number or null")
    elif kind == "list":
        if not isinstance(value, list):
            errors.append(f"{where} must be a list")
    elif kind == "dict":
        if not isinstance(value, dict):
            errors.append(f"{where} must be a dict")
    elif kind == "choices" and value not in choices:
        errors.append(f"{where} must be one of {'/'.join(choices)}, got {value!r}")


def _check_tools_expected(tools: dict, errors: list[str]) -> None:
    """Each expected tool entry needs at least a name; bounds are typed."""
    for i, entry in enumerate(tools.get("expected") or []):
        where = f"tools.expected[{i}]"
        if isinstance(entry, str):
            continue  # plain names are accepted shorthand
        if not isinstance(entry, dict):
            errors.append(f"{where} must be a tool name or object")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{where}.name must be a non-empty string")
        for bound_key in ("min_calls", "max_calls"):
            if bound_key in entry and not (
                entry[bound_key] is None or _is_int(entry[bound_key]) and entry[bound_key] >= 0
            ):
                errors.append(f"{where}.{bound_key} must be a non-negative integer or null")
        if "arguments" in entry and not isinstance(entry["arguments"], dict):
            errors.append(f"{where}.arguments must be a dict")
        if "arguments_mode" in entry and entry["arguments_mode"] not in ("any", "all"):
            errors.append(f"{where}.arguments_mode must be 'any' or 'all'")


def _check_list_of_strings(section: str, key: str, value: list, errors: list[str]) -> None:
    for i, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{section}.{key}[{i}] must be a string")


_STRING_LIST_FIELDS = {
    ("tools", "allowed"), ("tools", "forbidden"),
    ("retrieval", "sources"), ("retrieval", "forbidden_sources"),
    ("handoffs", "allowed"), ("handoffs", "forbidden"), ("handoffs", "required"),
    ("guardrails", "required"), ("guardrails", "must_pass"),
    ("approvals", "required_for"),
    ("policy", "allowed_data_scopes"), ("policy", "forbidden_data_scopes"),
    ("policy", "forbidden_side_effects"), ("policy", "allowed_destinations"),
    ("semantic_judge", "criteria"),
    ("trace", "required_kinds"),
}


def validate_agentic_metadata(metadata: dict) -> tuple[bool, list[str]]:
    """Validate agentic metadata schema v2.

    Returns (is_valid, list_of_errors).

    All sections are optional; only ``schema_version`` must be present for a
    v2 document.  Supplied sections and fields are strictly type-checked —
    absence means "not checked", an explicit false/zero/null is an assertion.
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

    for section, allowed in _SECTION_KEYS.items():
        value = metadata.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append(f"{section} must be a dict")
            continue
        errors.extend(
            f"Unknown field: {section}.{field}"
            for field in sorted(set(value) - allowed)
        )
        for key, field_value in value.items():
            if key in allowed:
                _check_field(section, key, field_value, errors)
        if section == "tools":
            _check_tools_expected(value, errors)
        for other_key in (k for k, v in value.items() if (section, k) in _STRING_LIST_FIELDS and isinstance(v, list)):
            _check_list_of_strings(section, other_key, value[other_key], errors)

    # Cross-field sanity: severity overrides map check-name -> severity.
    enforcement = metadata.get("enforcement")
    if isinstance(enforcement, dict) and isinstance(enforcement.get("severity_overrides"), dict):
        for override_name, severity in enforcement["severity_overrides"].items():
            if severity not in _SEVERITIES:
                errors.append(f"enforcement.severity_overrides.{override_name} must be one of {'/'.join(_SEVERITIES)}")

    if "rules" in metadata:
        from .rules import validate_rules

        for problem in validate_rules(metadata["rules"]):
            where = f"rules[{problem.index}]" if problem.index >= 0 else "rules"
            errors.append(f"{where}: {problem.message}")

    return len(errors) == 0, errors


def migrate_v1_to_v2(v1_metadata: dict) -> dict:
    """Migrate v1 metadata to v2 format (pure data transform, never mutates v1).

    v2 sections are sparse: only the sections present in the v1 document are
    carried over, plus ``schema_version``.  No dormant defaults are injected.
    """
    source = deepcopy(v1_metadata)
    v2: dict = {"schema_version": 2}
    for section, value in source.items():
        if section == "schema_version":
            continue
        v2[section] = value
    return v2


def get_schema_v2_template() -> dict:
    """Return a minimal valid v2 skeleton.

    Deliberately sparse: sections that do not apply to a scenario should stay
    omitted rather than being populated with dormant values.
    """
    return {
        "schema_version": 2,
        "enforcement": {"mode": "gating"},
    }
