"""Focused agentic metadata presets and individual constraint adders.

A preset emits ONLY the sections relevant to the audit focus — no dormant
fields.  Absence stays absence: a preset never fills a section just because
the schema allows it.  Individual adders let authors layer one specific
constraint at a time (each adder mutates the dict in place and returns it).
"""

from copy import deepcopy

# Canonical criteria vocabulary for semantic_judge.criteria.
SEMANTIC_CRITERIA = (
    "task_completion",
    "tool_selection",
    "tool_result_handling",
    "grounding",
    "process_safety",
    "efficiency",
)


def _base() -> dict:
    """Minimal v2 document every preset starts from."""
    return {
        "schema_version": 2,
        "trace": {"required": True, "content_capture": "structural", "missing_evidence": "inconclusive"},
        "enforcement": {"mode": "gating"},
    }


def base_metadata() -> dict:
    """Return a fresh minimal v2 skeleton (sparse, nothing asserted yet)."""
    return _base()


def _add_semantic(metadata: dict, *criteria: str) -> None:
    unknown = [c for c in criteria if c not in SEMANTIC_CRITERIA]
    if unknown:
        raise ValueError(f"Unknown semantic judge criteria: {', '.join(unknown)}")
    metadata["semantic_judge"] = {"enabled": True, "criteria": list(criteria)}


def _add_tool_expected(metadata: dict, name: str, *, arguments: dict | None = None,
                       min_calls: int = 1, max_calls: int | None = None,
                       arguments_mode: str = "any") -> None:
    tools = metadata.setdefault("tools", {})
    entry = {"name": name, "min_calls": min_calls}
    if max_calls is not None:
        entry["max_calls"] = max_calls
    if arguments is not None:
        entry["arguments"] = arguments
        entry["arguments_mode"] = arguments_mode
    expected = list(tools.get("expected") or [])
    existing = next((e for e in expected if isinstance(e, dict) and e.get("name") == name), None)
    if existing is None:
        expected.append(entry)
    else:
        expected[expected.index(existing)] = {**existing, **entry}
    tools["expected"] = expected


def _add_tool_forbidden(metadata: dict, *names: str) -> None:
    tools = metadata.setdefault("tools", {})
    forbidden = set(tools.get("forbidden") or [])
    forbidden.update(names)
    tools["forbidden"] = sorted(forbidden)


def preset_tool_accuracy(order_id: str, tool_name: str = "acme_get_order") -> dict:
    """Assert the exact tool call with its arguments; nothing else."""
    m = _base()
    _add_tool_expected(m, tool_name, arguments={"order_id": order_id})
    return m


def preset_retrieval_provenance(source: str, *, min_documents: int = 1) -> dict:
    """Assert retrieval actually happened against a named source."""
    m = _base()
    m["retrieval"] = {"required": True, "sources": [source], "min_documents": min_documents}
    return m


def preset_forbidden_actions(*tool_names: str, read_only: bool = False) -> dict:
    """Assert that mutation tools are never called (optionally read-only policy)."""
    m = _base()
    _add_tool_forbidden(m, *tool_names)
    if read_only:
        m["policy"] = {"read_only": True}
    return m


def preset_semantic_grounding(*criteria: str) -> dict:
    """Enable the semantic judge on grounding-oriented criteria only."""
    m = _base()
    _add_semantic(m, *(criteria or ("tool_result_handling", "grounding")))
    return m


def preset_recovery_budget(tool_name: str, *, max_calls: int = 2,
                           max_tool_calls: int | None = None) -> dict:
    """Bounded retry: cap calls of one tool and optionally the total budget."""
    m = _base()
    _add_tool_expected(m, tool_name, min_calls=1, max_calls=max_calls)
    if max_tool_calls is not None:
        m["budgets"] = {"max_tool_calls": max_tool_calls}
    return m


# ---------------------------------------------------------------------------
# Individual constraint adders (chainable: each returns the same dict)
# ---------------------------------------------------------------------------

def add_tool_expected(metadata: dict, name: str, **kwargs) -> dict:
    _add_tool_expected(metadata, name, **kwargs)
    return metadata


def add_tool_forbidden(metadata: dict, *names: str) -> dict:
    _add_tool_forbidden(metadata, *names)
    return metadata


def add_retrieval_required(metadata: dict, source: str, *, min_documents: int | None = None) -> dict:
    retrieval = metadata.setdefault("retrieval", {"required": True})
    sources = list(retrieval.get("sources") or [])
    if source not in sources:
        sources.append(source)
    retrieval["sources"] = sources
    retrieval["required"] = True
    if min_documents is not None:
        retrieval["min_documents"] = min_documents
    return metadata


def add_policy_read_only(metadata: dict) -> dict:
    metadata.setdefault("policy", {})["read_only"] = True
    return metadata


def add_policy_data_scopes(metadata: dict, *scopes: str) -> dict:
    policy = metadata.setdefault("policy", {})
    allowed = list(policy.get("allowed_data_scopes") or [])
    for scope in scopes:
        if scope not in allowed:
            allowed.append(scope)
    policy["allowed_data_scopes"] = allowed
    return metadata


def add_approval_required(metadata: dict, action: str) -> dict:
    approvals = metadata.setdefault("approvals", {"must_precede_execution": True})
    required_for = list(approvals.get("required_for") or [])
    if action not in required_for:
        required_for.append(action)
    approvals["required_for"] = required_for
    return metadata


def add_handoff_required(metadata: dict, agent: str, *, max_handoffs: int = 1) -> dict:
    handoffs = metadata.setdefault("handoffs", {"max_handoffs": max_handoffs})
    required = list(handoffs.get("required") or [])
    if agent not in required:
        required.append(agent)
    handoffs["required"] = required
    return metadata


def add_budget(metadata: dict, **limits) -> dict:
    budgets = metadata.setdefault("budgets", {})
    budgets.update(limits)
    return metadata


def add_semantic_judge(metadata: dict, *criteria: str) -> dict:
    _add_semantic(metadata, *criteria)
    return metadata


PRESETS = {
    "tool_accuracy": preset_tool_accuracy,
    "retrieval_provenance": preset_retrieval_provenance,
    "forbidden_actions": preset_forbidden_actions,
    "semantic_grounding": preset_semantic_grounding,
    "recovery_budget": preset_recovery_budget,
}


def build_preset(name: str, *args, **kwargs) -> dict:
    """Build a preset by name; deep-copied so callers can mutate safely."""
    try:
        factory = PRESETS[name]
    except KeyError:
        raise ValueError(f"Unknown preset: {name}. Available: {', '.join(sorted(PRESETS))}") from None
    result = factory(*args, **kwargs)
    return deepcopy(result)
