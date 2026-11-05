"""Standard-first normalization of OTEL agent spans."""
import json
from typing import Any

from .schema import AgentTrajectory, TrajectoryStep


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return span.get("attributes") or span.get("attrs") or {}


def _kind(name: str, attrs: dict[str, Any]) -> str:
    """Classify exclusively from OpenTelemetry GenAI semantic attributes."""
    operation = attrs.get("gen_ai.operation.name")
    mapping = {
        "chat": "inference",
        "generate_content": "inference",
        "text_completion": "inference",
        "embeddings": "embedding",
        "embedding": "embedding",
        "retrieval": "retrieval",
        "retriever": "retrieval",
        "retrieve": "retrieval",
        "rerank": "rerank",
        "execute_tool": "tool",
        "invoke_agent": "workflow",
        "invoke_workflow": "workflow",
        "create_agent": "workflow",
        "handoff": "handoff",
    }
    if operation is not None:
        return mapping.get(str(operation).lower(), "unknown")
    if attrs.get("gen_ai.tool.name"):
        return "tool"
    if attrs.get("gen_ai.retrieval.documents"):
        return "retrieval"
    return "unknown"


def _tool_arguments(attrs: dict[str, Any]) -> Any:
    raw = attrs.get("gen_ai.tool.call.arguments")
    if isinstance(raw, str):
        import json
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def _content(attrs: dict[str, Any], *keys: str) -> Any:
    """Return the first captured content field, parsing JSON strings."""

    raw = next((attrs.get(key) for key in keys if attrs.get(key) is not None), None)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


def inside_retrieval(spans: list[dict[str, Any]]) -> set:
    """Ids of the spans nested (at any depth) in a GenAI ``retrieval`` span.

    A retrieval span is a retrieval pipeline (OTel GenAI conventions); a model call
    the pipeline makes, such as rewriting the question into a search query, is its child.
    """
    parent = {s.get("span_id"): s.get("parent_span_id") for s in spans if isinstance(s, dict)}
    retrievals = {
        s.get("span_id") for s in spans
        if isinstance(s, dict) and _kind(str(s.get("name") or ""), _attrs(s)) == "retrieval"
    }
    found = set()
    for span_id in parent:
        seen, current = set(), parent.get(span_id)
        while current and current not in seen:
            if current in retrievals:
                found.add(span_id)
                break
            seen.add(current)
            current = parent.get(current)
    return found


def normalize(all_spans: list[dict[str, Any]]) -> AgentTrajectory:
    """Normalize the captured OTLP spans. The trajectory is only what was observed:
    there is no reconstruction from conversation text."""
    steps = []
    in_retrieval = inside_retrieval(all_spans)
    for index, span in enumerate(all_spans):
        attrs = _attrs(span)
        name = str(span.get("name") or span.get("span_name") or "unknown")
        purpose = str(attrs.get("purpose") or "primary")
        if purpose not in {"primary", "auxiliary"}:
            purpose = "auxiliary"
        kind = _kind(name, attrs)
        if kind == "inference" and span.get("span_id") in in_retrieval:
            # A model call inside a retrieval span (query rewriting) is a step of the
            # retrieval pipeline, not the agent's answer.
            purpose = "auxiliary"
        step_name = name
        tool_name = None
        if kind == "tool":
            tool_name = str(attrs.get("gen_ai.tool.name") or name)
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
            ),
            tool_name=tool_name,
            tool_call_id=attrs.get("gen_ai.tool.call.id"),
            arguments=_tool_arguments(attrs) if kind == "tool" else None,
            result=_content(attrs, "gen_ai.tool.call.result") if kind == "tool" else None,
            handoff_to=(
                attrs.get("gen_ai.handoff.to")
                or attrs.get("handoff.to")
            ),
            attributes=attrs,
            events=span.get("events") or [],
        ))
    return AgentTrajectory(steps=steps)
