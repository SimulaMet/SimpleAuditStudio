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
    for span in _ordered([s for s in out if isinstance(s, dict)]):
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
