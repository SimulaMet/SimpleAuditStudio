"""Standard-first normalization of OTEL agent spans."""
import json
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


def from_conversation(conversation: list[dict[str, Any]] | None) -> AgentTrajectory:
    """Build a trajectory from the recorded conversation.

    Used as a fallback when no OTLP spans were captured (e.g. bare-model
    targets on SimulaChat that don't export traces). Parses native
    ``tool_calls``, ``[TOOL_CALL name(args)]`` markers, and
    ``[TOOL_RESULT name]{...}`` markers that Studio's streaming adapter
    injects into the content. Also captures the final assistant text as an
    inference step so the semantic judge can evaluate task completion.
    """
    steps: list[TrajectoryStep] = []
    index = 0
    for msg in conversation or []:
        role = msg.get("role", "")
        if role != "assistant":
            continue
        # Native OpenAI tool_calls field.
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name", "unknown")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, TypeError):
                args = raw_args
            steps.append(TrajectoryStep(
                span_id=f"conv-tool-{index}",
                kind="tool",
                name=name,
                tool_name=name,
                tool_call_id=tc.get("id"),
                arguments=args,
                index=index,
            ))
            index += 1
        # Parse [TOOL_CALL] and [TOOL_RESULT] markers from content.
        content = msg.get("content") or ""
        tool_steps = _conversation_tool_steps([msg])
        for ts in tool_steps:
            ts.index = index
            steps.append(ts)
            index += 1
        # Capture the final text response (after all tool markers) as an
        # inference step so the semantic judge sees the agent's answer.
        # In Studio's serialization, tool markers + results are concatenated
        # at the start, followed by a blank line, then the model's text.
        import re as _re
        stripped = ""
        # Primary: split on the first blank line (double newline).
        parts = _re.split(r"\n\s*\n", content, maxsplit=1)
        if len(parts) > 1:
            candidate = parts[1].strip()
            # Verify it doesn't look like pure JSON (tool result leak).
            if candidate and not candidate.startswith(("{", "[")):
                stripped = candidate
        # Fallback: strip all [TOOL_*] markers and their immediate JSON.
        if not stripped or len(stripped) < 20:
            no_markers = _re.sub(
                r"\[(?:TOOL_CALL|TOOL_RESULT)\s+\w+(?:\([^)]*\))?\][^\n]*",
                "", content,
            ).strip()
            if no_markers and not no_markers.startswith(("{", "[")):
                stripped = no_markers
        if stripped and len(stripped) > 20:
            steps.append(TrajectoryStep(
                span_id=f"conv-response-{index}",
                kind="inference",
                name="final_response",
                result=stripped[:2000],
                index=index,
            ))
            index += 1
    return AgentTrajectory(steps=steps)


def _conversation_tool_steps(conversation: list[dict[str, Any]]) -> list[TrajectoryStep]:
    """Extract tool-call steps from [TOOL_CALL]/[TOOL_RESULT] markers.

    Used to merge locally-executed tools (which may not emit OTLP spans) into
    a trajectory that already has OTLP-derived steps. Also captures tool
    results so the semantic judge can evaluate grounding.
    """
    import json as _json
    import re

    # Parse all markers in order to pair calls with results.
    steps: list[TrajectoryStep] = []
    pending_calls: dict[str, TrajectoryStep] = {}  # name+args_sig -> step
    index = 0
    for msg in conversation or []:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or ""
        # Match both TOOL_CALL and TOOL_RESULT markers in document order.
        pat_call = re.compile(r"\[TOOL_CALL\s+(\w+)\(([^)]*)\)\]")
        # Result payload ends at the next [TOOL_ marker or blank line.
        pat_result = re.compile(r"\[TOOL_RESULT\s+(\w+)\]((?:(?!\[TOOL_).)*?)(?=\[TOOL_|\n\s*\n|$)")
        events: list[tuple[int, str, str, str]] = []  # (pos, type, name, payload)
        for m in pat_call.finditer(content):
            events.append((m.start(), "call", m.group(1), m.group(2).strip()))
        for m in pat_result.finditer(content):
            events.append((m.start(), "result", m.group(1), m.group(2)))
        events.sort(key=lambda e: e[0])
        for pos, etype, name, payload in events:
            if etype == "call":
                try:
                    args = _json.loads(payload) if payload else {}
                except (_json.JSONDecodeError, TypeError):
                    args = {"raw": payload}
                step = TrajectoryStep(
                    span_id=f"conv-tool-{index}",
                    kind="tool",
                    name=name,
                    tool_name=name,
                    arguments=args,
                    index=index,
                )
                steps.append(step)
                pending_calls[(name, _json.dumps(args, sort_keys=True, default=str))] = step
                index += 1
            elif etype == "result":
                # Find the matching call by name (most recent unmatched).
                result_val = payload.strip()
                matched = False
                for key, step in reversed(list(pending_calls.items())):
                    if key[0] == name and step.result is None:
                        try:
                            parsed = _json.loads(result_val) if result_val else None
                        except (_json.JSONDecodeError, TypeError):
                            parsed = result_val
                        step.result = parsed
                        # Also populate the OTLP-style attribute so checks
                        # that read gen_ai.tool.call.result can find it.
                        step.attributes["gen_ai.tool.call.result"] = parsed
                        del pending_calls[key]
                        matched = True
                        break
                if not matched:
                    # No matching call; create a result-only step.
                    steps.append(TrajectoryStep(
                        span_id=f"conv-tool-result-{index}",
                        kind="tool",
                        name=name,
                        tool_name=name,
                        result=result_val,
                        index=index,
                    ))
                    index += 1
    return steps


def normalize(all_spans: list[dict[str, Any]],
              conversation: list[dict[str, Any]] | None = None) -> AgentTrajectory:
    """Normalize OTLP spans; merge conversation tool markers for local tools."""
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
            # Providers like Open WebUI emit no native data-scope attribute,
            # but a tool call that accessed a specific order still leaves
            # concrete evidence: synthesize the scope from the argument so
            # policy.data_scope has something real to check.
            if not attrs.get("data_scope"):
                args = _tool_arguments(attrs)
                if isinstance(args, dict):
                    order_id = args.get("order_id")
                    if isinstance(order_id, str) and order_id:
                        attrs = {**attrs, "data_scope": [f"order:{order_id}"]}
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
    # When no OTLP spans were captured at all (bare-model targets that don't
    # export traces), derive the entire trajectory from the conversation.
    if not steps and conversation:
        return from_conversation(conversation)

    # Merge: even when OTLP spans exist, locally-executed tools (Studio's
    # execution loop) may not emit their own spans. Parse [TOOL_CALL] markers
    # from the conversation and append any tool steps not already present in
    # the OTLP-derived steps (matched by name + argument signature).
    if conversation:
        existing_sigs = {
            (s.name, json.dumps(s.arguments, sort_keys=True, default=str) if s.arguments else "")
            for s in steps if s.kind == "tool"
        }
        conv_steps = _conversation_tool_steps(conversation)
        for cs in conv_steps:
            sig = (cs.name, json.dumps(cs.arguments, sort_keys=True, default=str) if cs.arguments else "")
            if sig not in existing_sigs:
                cs.index = len(steps)
                steps.append(cs)
                existing_sigs.add(sig)

    return AgentTrajectory(steps=steps)
