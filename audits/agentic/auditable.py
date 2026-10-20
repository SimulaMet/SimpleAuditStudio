"""Keep only the OTLP spans an agentic audit needs, labelled with their role.

Targets export every span they produce (HTTP plumbing, DB calls, framework
chains). Auditing only reads a small set of step kinds, so spans outside that
set are dropped at ingestion, and the rest are tagged with an OpenInference
``span.kind`` so the engine's evidence selection ranks them correctly.
"""
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

# Step kind -> OpenInference kind understood by the engine's select_spans.
_OPENINFERENCE_KIND = {
    "inference": "LLM",
    "tool": "TOOL",
    "retrieval": "RETRIEVER",
    "rerank": "RETRIEVER",
    "guardrail": "GUARDRAIL",
    "workflow": "AGENT",
    "handoff": "AGENT",
}

_TOOL_NAME_KEYS = ("gen_ai.tool.name", "openwebui.tool.name", "tool.name")
_RETRIEVAL_KEYS = ("openwebui.retrieval.data_source", "gen_ai.retrieval.documents")
_EXPLICIT_KIND_KEYS = (
    "gen_ai.operation.name", "openinference.span.kind", "openwebui.span.kind",
)


def classify_span(span: Mapping[str, Any]) -> str:
    """Return the audit step kind for a raw span, or ``"unknown"``."""
    attrs = span.get("attributes") or {}
    if any(attrs.get(key) for key in _TOOL_NAME_KEYS):
        return "tool"
    if any(attrs.get(key) for key in _RETRIEVAL_KEYS):
        return "retrieval"
    if not any(attrs.get(key) for key in _EXPLICIT_KIND_KEYS):
        # Transport spans ("POST /api/v1/chat/completions http send") mention
        # model words in their names. Without an explicit kind attribute a span
        # is not an audit step, so the name is never used to classify it.
        return "unknown"
    return _kind(str(span.get("name") or ""), attrs)


def select_auditable_spans(spans: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop non-audit spans and tag the rest with their OpenInference kind."""
    kept: list[dict[str, Any]] = []
    for raw in spans:
        span = dict(raw)
        step_kind = classify_span(span)
        if step_kind not in AUDITABLE_STEP_KINDS:
            continue
        attrs = dict(span.get("attributes") or {})
        openinference = _OPENINFERENCE_KIND.get(step_kind)
        if openinference and not attrs.get("openinference.span.kind"):
            attrs["openinference.span.kind"] = openinference
        span["attributes"] = attrs
        if openinference:
            span["kind"] = attrs.get("openinference.span.kind", openinference)
        kept.append(span)
    return kept
