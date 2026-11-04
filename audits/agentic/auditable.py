"""Select audit-relevant spans from OpenTelemetry GenAI attributes only."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .trajectory import _kind

# Step kinds the agentic checks read. Anything else is connection or framework
# plumbing and is not stored.
AUDITABLE_STEP_KINDS = frozenset({
    "inference", "tool", "retrieval", "rerank", "guardrail",
    "approval", "handoff", "workflow", "state",
})

# Internal audit categories are not telemetry semantic-convention attributes.
_INTERNAL_KIND = {
    "inference": "LLM",
    "tool": "TOOL",
    "retrieval": "RETRIEVER",
    "rerank": "RERANKER",
    "guardrail": "GUARDRAIL",
    "workflow": "AGENT",
    "handoff": "AGENT",
}
_TOOL_NAME_KEYS = ("gen_ai.tool.name",)
_RETRIEVAL_KEYS = ("gen_ai.retrieval.documents",)


def classify_span(span: Mapping[str, Any]) -> str:
    """Return the audit step kind for a raw span, or ``"unknown"``."""
    attrs = span.get("attributes") or {}
    if any(attrs.get(key) for key in _TOOL_NAME_KEYS):
        return "tool"
    if any(attrs.get(key) for key in _RETRIEVAL_KEYS):
        return "retrieval"
    if not attrs.get("gen_ai.operation.name"):
        # Transport spans ("POST /api/v1/chat/completions http send") mention
        # model words in their names. Without an explicit kind attribute a span
        # is not an audit step, so the name is never used to classify it.
        return "unknown"
    return _kind(str(span.get("name") or ""), attrs)


def select_auditable_spans(spans: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep GenAI audit spans, retaining source attributes unchanged."""
    kept: list[dict[str, Any]] = []
    for raw in spans:
        span = dict(raw)
        step_kind = classify_span(span)
        if step_kind not in AUDITABLE_STEP_KINDS:
            continue
        attrs = dict(span.get("attributes") or {})
        span["attributes"] = attrs
        if step_kind in _INTERNAL_KIND:
            span["kind"] = _INTERNAL_KIND[step_kind]
        kept.append(span)
    return kept
