"""Reusable separation of fetched trace evidence from selected evidence."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceEvidenceBundle:
    """All fetched spans plus the engine-selected judging subset and diagnostics."""

    all_spans: list[dict[str, Any]] = field(default_factory=list)
    selected_spans: list[dict[str, Any]] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
