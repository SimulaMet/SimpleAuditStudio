"""Strict validation for agentic scenario metadata: the sections the form owns."""
from dataclasses import dataclass
from typing import Any

_KEYS = {"schema_version", "trace", "goal", "semantic_judge", "enforcement", "rules"}


@dataclass(frozen=True)
class AgenticExpectations:
    schema_version: int
    enforcement: dict[str, Any]
    trace: dict[str, Any] | None = None
    goal: dict[str, Any] | None = None
    semantic_judge: dict[str, Any] | None = None
    rules: list[dict[str, Any]] | None = None


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
    rules = agentic.get("rules") or []
    if rules:
        from .rules import validate_rules

        problems = validate_rules(rules)
        if problems:
            raise ValueError("Invalid metadata.agentic.rules: " + "; ".join(
                f"rule {p.index + 1}: {p.message}" for p in problems))
    return AgenticExpectations(
        schema_version=agentic["schema_version"],
        enforcement=agentic.get("enforcement", {}),
        trace=agentic.get("trace"),
        goal=agentic.get("goal"),
        semantic_judge=agentic.get("semantic_judge"),
        rules=rules,
    )
