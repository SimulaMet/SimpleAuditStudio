"""Compact GenAI span content: each piece of content is stored once per trace.

GenAI spans are self-contained, so a tool loop or a multi-turn conversation
repeats the system prompt, the history and every earlier tool result on each
model call. Studio stores spans compacted with the same reference format as
Open WebUI's compact mode (its ``OTEL_GENAI_CONTENT_MODE`` default), whatever
the target sent:

* a repeated chat message becomes ``{"role": <role>, "parts": [{"type":
  "reference", "span_id": <hex>, "attribute": <key>, "index": <i>}]}``;
* a repeated value (a tool result, a tool response inside a message, system
  instructions, tool arguments) becomes ``{"type": "reference", "span_id": <hex>,
  "attribute": <key>}``.

References always point to an earlier span of the same trace. ``expand_spans``
resolves them back to full content (for rules, the judge and exports);
``compact_spans`` is idempotent and keeps already-compact content as it is.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

INPUT = "gen_ai.input.messages"
OUTPUT = "gen_ai.output.messages"
SYSTEM = "gen_ai.system_instructions"
TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
TOOL_RESULT = "gen_ai.tool.call.result"
DOCUMENTS = "gen_ai.retrieval.documents"
_MESSAGE_ATTRIBUTES = (INPUT, OUTPUT)
_VALUE_ATTRIBUTES = (SYSTEM, TOOL_ARGUMENTS, TOOL_RESULT, DOCUMENTS)

# Short values cost less inline than as a reference (same threshold as Open WebUI).
_MIN_LENGTH = 64
_MAX_DEPTH = 8
_WRAPPER_OPERATIONS = {"invoke_workflow"}


def _parsed(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _key(value: Any) -> str | None:
    try:
        text = json.dumps(_parsed(value), sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if len(text) >= _MIN_LENGTH else None


def _message_shape(message: Any) -> Any:
    """A message as compared across spans: no finish reason, no empty text parts."""
    if not isinstance(message, dict):
        return message
    parts = [
        part for part in message.get("parts") or []
        if not (isinstance(part, dict) and part.get("type") == "text" and not part.get("content"))
    ]
    shape = {"role": message.get("role"), "parts": parts}
    if message.get("name"):
        shape["name"] = message["name"]
    return shape


def is_reference(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "reference" and bool(value.get("span_id"))


def message_reference(message: Any) -> dict | None:
    """The reference a compacted message stands for, or None for a real message."""
    if isinstance(message, dict):
        parts = message.get("parts") or []
        if len(parts) == 1 and is_reference(parts[0]):
            return parts[0]
    return None


def _ordered(spans: list[dict]) -> list[dict]:
    def start(span: dict) -> float:
        value = span.get("start_time")
        return float(value) if isinstance(value, (int, float)) else 0.0
    return sorted(spans, key=start)  # stable: equal start times keep their order


def _attrs(span: dict) -> dict:
    attrs = span.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _store(attrs: dict, key: str, original: Any, value: Any) -> None:
    attrs[key] = json.dumps(value, ensure_ascii=False, default=str) if isinstance(original, str) else value


def _found(registry: dict, span_id: str, value: Any) -> dict | None:
    """Where the trace first recorded ``value`` on another span, or None."""
    key = _key(value)
    location = registry.get(key) if key else None
    return location if location and location["span_id"] != span_id else None


def _remember(registry: dict, value: Any, location: dict) -> None:
    key = _key(value)
    if key and key not in registry:
        registry[key] = location


def _compact_values(attrs: dict, registry: dict, span_id: str) -> None:
    for attribute in _VALUE_ATTRIBUTES:
        if attribute not in attrs:
            continue
        value = _parsed(attrs[attribute])
        if is_reference(value):
            continue
        location = {"span_id": span_id, "attribute": attribute}
        inner = value.get("content") if isinstance(value, dict) and set(value) == {"content"} else None
        hit = _found(registry, span_id, value) or (
            _found(registry, span_id, inner) if inner is not None else None)
        # Retrieval documents stay inline: rules and other tools read them there.
        if hit and attribute != DOCUMENTS:
            _store(attrs, attribute, attrs[attribute], {"type": "reference", **hit})
            continue
        _remember(registry, value, location)
        if inner is not None:
            _remember(registry, inner, location)


def _compact_tool_message(message: dict, registry: dict, span_id: str) -> dict:
    parts = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("type") == "tool_call_response" \
                and not is_reference(part.get("response")):
            hit = _found(registry, span_id, part.get("response"))
            if hit:
                part = {**part, "response": {"type": "reference", **hit}}
        parts.append(part)
    return {**message, "parts": parts}


def _compact_messages(attrs: dict, registry: dict, span_id: str) -> None:
    for attribute in _MESSAGE_ATTRIBUTES:
        messages = _parsed(attrs.get(attribute))
        if not isinstance(messages, list):
            continue
        compacted = []
        for index, message in enumerate(messages):
            if message_reference(message):
                compacted.append(message)
                continue
            hit = _found(registry, span_id, _message_shape(message)) if attribute == INPUT else None
            if hit:
                role = message.get("role") if isinstance(message, dict) else None
                compacted.append({"role": role, "parts": [{"type": "reference", **hit}]})
                continue
            _remember(registry, _message_shape(message), {"span_id": span_id, "attribute": attribute, "index": index})
            if isinstance(message, dict) and message.get("role") == "tool":
                message = _compact_tool_message(message, registry, span_id)
            compacted.append(message)
        _store(attrs, attribute, attrs[attribute], compacted)


def compact_spans(spans: list[dict]) -> list[dict]:
    """Spans (copies, same order) with content already recorded in the trace replaced by references."""
    out = [copy.deepcopy(s) if isinstance(s, dict) else s for s in spans]
    registries: dict[str, dict[str, dict]] = {}   # trace id -> content key -> location
    ordered = _ordered([s for s in out if isinstance(s, dict)])
    # Retrieval documents are the standard home of retrieved chunks: register them
    # first, so the enclosing tool's result and the later tool response refer to
    # them (the tool span starts before its retrieval child).
    for span in ordered:
        attrs = _attrs(span)
        if span.get("span_id") and DOCUMENTS in attrs and not is_reference(_parsed(attrs[DOCUMENTS])):
            registry = registries.setdefault(str(span.get("trace_id") or ""), {})
            _remember(registry, _parsed(attrs[DOCUMENTS]), {"span_id": str(span["span_id"]), "attribute": DOCUMENTS})
    for span in ordered:
        span_id = str(span.get("span_id") or "")
        if not span_id:
            continue
        attrs = _attrs(span)
        # A workflow wrapper (one per user turn) repeats the turn's input; content is
        # kept on the model/tool spans, never referenced to a wrapper.
        if str(attrs.get("gen_ai.operation.name") or "").lower() in _WRAPPER_OPERATIONS:
            continue
        registry = registries.setdefault(str(span.get("trace_id") or ""), {})
        _compact_values(attrs, registry, span_id)
        _compact_messages(attrs, registry, span_id)
    return out


def expand_spans(spans: list[dict], pool: list[dict] | None = None) -> list[dict]:
    """Spans (copies, same order) with every reference resolved to full content.

    ``pool`` holds spans a reference may point to beyond ``spans`` themselves
    (e.g. the full capture when ``spans`` is an evidence subset). An unresolvable
    reference is left in place.
    """
    index = {}
    for span in list(pool or []) + list(spans):
        if isinstance(span, dict) and span.get("span_id"):
            index.setdefault(str(span["span_id"]), _attrs(span))

    def target(reference: dict, depth: int) -> Any:
        attrs = index.get(str(reference.get("span_id")))
        if attrs is None or depth > _MAX_DEPTH:
            return reference
        value = _parsed(attrs.get(reference.get("attribute")))
        position = reference.get("index")
        if isinstance(position, int):
            if not isinstance(value, list) or not 0 <= position < len(value):
                return reference
            return resolve_message(value[position], depth + 1)
        return resolve(value, depth + 1)

    def resolve(value: Any, depth: int) -> Any:
        return target(value, depth) if is_reference(value) else value

    def resolve_message(message: Any, depth: int) -> Any:
        reference = message_reference(message)
        if reference:
            resolved = target(reference, depth)
            return message if resolved is reference else resolved
        if isinstance(message, dict) and message.get("role") == "tool":
            return {**message, "parts": [
                {**part, "response": resolve(part.get("response"), depth)}
                if isinstance(part, dict) and part.get("type") == "tool_call_response" else part
                for part in message.get("parts") or []
            ]}
        return message

    out = []
    for span in spans:
        if not isinstance(span, dict):
            out.append(span)
            continue
        span = copy.deepcopy(span)
        attrs = _attrs(span)
        for attribute in _VALUE_ATTRIBUTES:
            if attribute in attrs and is_reference(_parsed(attrs[attribute])):
                _store(attrs, attribute, attrs[attribute], resolve(_parsed(attrs[attribute]), 0))
        for attribute in _MESSAGE_ATTRIBUTES:
            messages = _parsed(attrs.get(attribute))
            if isinstance(messages, list):
                _store(attrs, attribute, attrs[attribute], [resolve_message(m, 0) for m in messages])
        out.append(span)
    return out


# OpenTelemetry's error fields: what failed, and the exception message.
_JUDGE_ERROR_KEYS = {"error.type", "exception.type", "exception.message"}
# What a judge must see first: a judge prompt bounds each span, so the agent's
# decisions and observations go before the (long) input history.
_JUDGE_FIRST = (
    "gen_ai.operation.name", "gen_ai.tool.name", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
    "gen_ai.retrieval.query.text", "gen_ai.retrieval.documents", "gen_ai.output.messages",
    "gen_ai.response.finish_reasons", "error.type", "exception.message",
)


def _judge_marker(reference: dict, role: str | None = None) -> str:
    where = str(reference.get("span_id") or "")[:8]
    what = str(reference.get("attribute") or "").removeprefix("gen_ai.")
    if isinstance(reference.get("index"), int):
        what += f" message {reference['index'] + 1}"
    return f"(same as span {where} {what}" + (f", {role})" if role else ")")


def _judge_markers(value: Any) -> Any:
    reference = message_reference(value)
    if reference:
        return _judge_marker(reference, value.get("role"))
    if is_reference(value):
        return _judge_marker(value)
    if isinstance(value, list):
        return [_judge_markers(item) for item in value]
    if isinstance(value, dict):
        return {key: _judge_markers(item) for key, item in value.items()}
    return value


def _without_rag_template(messages: Any) -> Any:
    """User messages without a RAG prompt template (fixed instructions + ``<context>``).

    Open WebUI rewrites the user's message with its RAG template on every model
    call; the retrieved sources are on the retrieval spans already.
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for message in messages:
        parts = message.get("parts") if isinstance(message, dict) else None
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(parts, list):
            new_parts = []
            for part in parts:
                text = part.get("content") if isinstance(part, dict) and part.get("type") == "text" else None
                head, marker, tail = text.rpartition("</context>") if isinstance(text, str) else ("", "", "")
                if marker and "<context>" in head and tail.strip():
                    part = {**part, "content": f"{tail.strip()} (RAG template and <context> sources omitted)"}
                new_parts.append(part)
            message = {**message, "parts": new_parts}
        out.append(message)
    return out


def _store_value(original: Any, value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False, default=str) if isinstance(original, str) else value


def _pointing_to_conversation(messages: Any, said: dict[str, set[str]]) -> Any:
    """Messages whose text the judge already reads in the conversation, as a short note.

    Tool calls (and any non-text part) stay: only the repeated text is replaced.
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for message in messages:
        parts = message.get("parts") if isinstance(message, dict) else None
        role = message.get("role") if isinstance(message, dict) else None
        if isinstance(parts, list) and role in said:
            text = "".join(p.get("content") or "" for p in parts if isinstance(p, dict) and p.get("type") == "text")
            if text.strip() and text.strip() in said[role]:
                note = {"type": "text", "content": f"(the {role} turn shown in the conversation)"}
                message = {**message, "parts": [note] + [p for p in parts
                                                         if not (isinstance(p, dict) and p.get("type") == "text")]}
        out.append(message)
    return out


def judge_view(spans: list[dict], pool: list[dict] | None = None,
               conversation: list[dict] | None = None) -> list[dict]:
    """Spans as a trace-reading judge should see them: each piece of content once.

    Only ``gen_ai.*`` attributes and the error fields (``error.type``,
    ``exception.type``, ``exception.message``) are kept; workflow wrappers
    (their input is the conversation the judge already has) are dropped; content
    repeated from an earlier span becomes a short "(same as span …)" note, and a
    message the judge already reads in ``conversation`` a "(… shown in the
    conversation)" note; decisions, tool results and documents come before the input
    history so a per-span length bound never cuts them off.
    """
    said: dict[str, set[str]] = {}
    for turn in conversation or []:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), str):
            said.setdefault(turn["role"], set()).add(turn["content"].strip())
    pool = list(pool or [])
    shown = {s.get("span_id") for s in spans if isinstance(s, dict)}
    pooled = {s.get("span_id") for s in pool if isinstance(s, dict)}
    full = expand_spans(pool + [s for s in spans if isinstance(s, dict) and s.get("span_id") not in pooled], pool=pool)
    for span in full:
        attrs = _attrs(span)
        if INPUT in attrs:
            _store(attrs, INPUT, attrs[INPUT], _without_rag_template(_parsed(attrs[INPUT])))
    compacted = {s.get("span_id"): s for s in compact_spans(full)}
    out = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        span = copy.deepcopy(compacted.get(span.get("span_id"), span))
        attrs = _attrs(span)
        if str(attrs.get("gen_ai.operation.name") or "").lower() in _WRAPPER_OPERATIONS:
            continue
        # Only the GenAI semantic attributes and the standard error fields reach a judge.
        kept = {key: value for key, value in attrs.items() if key.startswith("gen_ai.") or key in _JUDGE_ERROR_KEYS}
        for key in (INPUT, OUTPUT):
            if key in kept and said:
                kept[key] = _store_value(kept[key], _pointing_to_conversation(_parsed(kept[key]), said))
        for key, value in list(kept.items()):
            parsed = _parsed(value)
            if isinstance(parsed, (list, dict)):
                marked = _judge_markers(parsed)
                if marked != parsed:
                    kept[key] = json.dumps(marked, ensure_ascii=False, default=str) if isinstance(value, str) else marked
        ordered = {key: kept[key] for key in _JUDGE_FIRST if key in kept}
        ordered.update({key: value for key, value in kept.items() if key not in ordered})
        span["attributes"] = ordered
        out.append(span)
    return [s for s in out if s.get("span_id") in shown]
