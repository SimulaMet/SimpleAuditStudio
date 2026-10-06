"""Versioned, strict validation for agentic scenario metadata."""
from dataclasses import dataclass
from typing import Any

_KEYS = {"schema_version", "tools", "retrieval", "rerank", "policy", "budgets", "trajectory", "enforcement"}


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


def validate_agentic_metadata(metadata: dict | None) -> AgenticExpectations | None:
    agentic = (metadata or {}).get("agentic")
    if agentic is None:
        return None
    if not isinstance(agentic, dict):
        raise TypeError("metadata.agentic must be an object")
    unknown = set(agentic) - _KEYS
    if unknown:
        raise ValueError(f"Unknown metadata.agentic keys: {', '.join(sorted(unknown))}")
    if agentic.get("schema_version") != 1:
        raise ValueError("metadata.agentic.schema_version must be 1")
    return AgenticExpectations(**{key: agentic.get(key, {}) for key in _KEYS})
