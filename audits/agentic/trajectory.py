"""Standard-first normalization of OTEL agent spans."""
from typing import Any

from .schema import AgentTrajectory, TrajectoryStep


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return span.get("attributes") or span.get("attrs") or {}


def _kind(name: str, attrs: dict[str, Any]) -> str:
    value = (
        attrs.get("gen_ai.operation.name")
        or attrs.get("openinference.span.kind")
        or attrs.get("openwebui.span.kind")
    )
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
            "handoff": "handoff",
            "transfer": "handoff",
        }
        return mapping.get(value, value)
    lowered = name.lower()
    for token, kind in (("embed", "embedding"), ("rerank", "rerank"), ("retriev", "retrieval"),
                        ("tool", "tool"), ("chat", "inference"), ("llm", "inference"),
                        ("workflow", "workflow"), ("agent", "workflow")):
        if token in lowered:
            return kind
    return "unknown"


def _tool_arguments(attrs: dict[str, Any]) -> Any:
    raw = attrs.get("gen_ai.tool.call.arguments") or attrs.get("tool.arguments")
    if isinstance(raw, str):
        import json
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _content(attrs: dict[str, Any], *keys: str) -> Any:
    """Return the first captured content field, parsing JSON strings."""
    import json

    raw = next((attrs.get(key) for key in keys if attrs.get(key) is not None), None)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def normalize(all_spans: list[dict[str, Any]]) -> AgentTrajectory:
    steps = []
    for index, span in enumerate(all_spans):
        attrs = _attrs(span)
        name = str(span.get("name") or span.get("span_name") or "unknown")
        purpose = str(attrs.get("purpose") or "primary")
        if purpose not in {"primary", "auxiliary"}:
            purpose = "auxiliary"
        kind = _kind(name, attrs)
        step_name = name
        tool_name = None
        if kind == "tool":
            tool_name = str(attrs.get("gen_ai.tool.name") or attrs.get("tool.name") or name)
            step_name = tool_name
        steps.append(TrajectoryStep(
            trace_id=str(span.get("trace_id") or ""),
            span_id=str(span.get("span_id") or ""),
            parent_span_id=span.get("parent_span_id"),
            index=index,
            kind=kind,
            name=step_name,
            purpose=purpose,
            status=span.get("status") or attrs.get("status"),
            start_time=span.get("start_time"),
            end_time=span.get("end_time"),
            duration_ms=(
                (float(span["end_time"]) - float(span["start_time"])) * 1000
                if span.get("start_time") is not None and span.get("end_time") is not None
                else None
            ),
            actor=(
                attrs.get("gen_ai.agent.name")
                or attrs.get("agent.name")
                or attrs.get("openinference.agent.name")
            ),
            tool_name=tool_name,
            tool_call_id=attrs.get("gen_ai.tool.call.id"),
            arguments=_tool_arguments(attrs) if kind == "tool" else None,
            result=_content(attrs, "gen_ai.tool.call.result", "tool.result") if kind == "tool" else None,
            handoff_to=(
                attrs.get("gen_ai.handoff.to")
                or attrs.get("handoff.to")
                or attrs.get("openinference.handoff.destination")
            ),
            attributes=attrs,
            events=span.get("events") or [],
        ))
    return AgentTrajectory(steps=steps)
