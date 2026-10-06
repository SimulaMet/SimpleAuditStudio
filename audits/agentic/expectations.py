"""Versioned, strict validation for agentic scenario metadata."""
from dataclasses import dataclass
from typing import Any

_KEYS = {
    "schema_version", "trace", "goal", "tools", "retrieval", "rerank", "policy",
    "budgets", "trajectory", "handoffs", "guardrails", "approvals", "state",
    "semantic_judge", "enforcement",
}


@dataclass(frozen=True)
class AgenticExpectations:
    schema_version: int
    tools: dict[str, Any]
    retrieval: dict[str, Any]
    rerank: dict[str, Any]
    policy: dict[str, Any]
    budgets: dict[str, Any]
    trajectory: dict[str, Any]
    enforcement: dict[str, Any]
    trace: dict[str, Any] | None = None
    goal: dict[str, Any] | None = None
    handoffs: dict[str, Any] | None = None
    guardrails: dict[str, Any] | None = None
    approvals: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    semantic_judge: dict[str, Any] | None = None


def validate_agentic_metadata(metadata: dict | None) -> AgenticExpectations | None:
    agentic = (metadata or {}).get("agentic")
    if agentic is None:
        return None
    if not isinstance(agentic, dict):
        raise TypeError("metadata.agentic must be an object")
    unknown = set(agentic) - _KEYS
    if unknown:
        raise ValueError(f"Unknown metadata.agentic keys: {', '.join(sorted(unknown))}")
    if agentic.get("schema_version") not in (1, 2):
        raise ValueError("metadata.agentic.schema_version must be 1 or 2")
    return AgenticExpectations(**{
        key: agentic.get(key, {}) for key in AgenticExpectations.__dataclass_fields__
    })
