"""Standard-first normalization of OTEL agent spans."""
from typing import Any

from .schema import AgentTrajectory, TrajectoryStep


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return span.get("attributes") or span.get("attrs") or {}


def _kind(name: str, attrs: dict[str, Any]) -> str:
    value = attrs.get("gen_ai.operation.name") or attrs.get("openinference.span.kind")
    if value:
        value = str(value).lower()
        mapping = {
            "chat": "inference",
            "llm": "inference",
            "text_completion": "inference",
            "embedding": "embedding",
            "embeddings": "embedding",
            "retriever": "retrieval",
            "retrieve": "retrieval",
            "reranker": "rerank",
            "rerank": "rerank",
            "tool": "tool",
            "execute_tool": "tool",
            "agent": "workflow",
            "create_agent": "workflow",
            "invoke_agent": "workflow",
            "invoke_workflow": "workflow",
            "chain": "workflow",
        }
        return mapping.get(value, value)
    lowered = name.lower()
    for token, kind in (("embed", "embedding"), ("rerank", "rerank"), ("retriev", "retrieval"),
                        ("tool", "tool"), ("chat", "inference"), ("llm", "inference"),
                        ("workflow", "workflow"), ("agent", "workflow")):
        if token in lowered:
            return kind
    return "unknown"


def normalize(all_spans: list[dict[str, Any]]) -> AgentTrajectory:
    steps = []
    for span in all_spans:
        attrs = _attrs(span)
        name = str(span.get("name") or span.get("span_name") or "unknown")
        purpose = str(attrs.get("purpose") or "primary")
        if purpose not in {"primary", "auxiliary"}:
            purpose = "auxiliary"
        kind = _kind(name, attrs)
        step_name = name
        if kind == "tool":
            step_name = str(attrs.get("gen_ai.tool.name") or attrs.get("tool.name") or name)
        steps.append(TrajectoryStep(
            span_id=span.get("span_id"), kind=kind, name=step_name,
            purpose=purpose, status=span.get("status") or attrs.get("status"),
            attributes=attrs, payload=span.get("events") or {},
        ))
    return AgentTrajectory(steps=steps)
