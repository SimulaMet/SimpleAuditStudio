"""Privacy-safe trace capture and serialization helpers."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

CAPTURE_LEVELS = {"structural", "inputs", "full"}
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|auth[_-]?header|cookie|set-cookie|"
    r"password|passwd|secret|token|credential|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+|basic\s+)[A-Za-z0-9+/=._~-]+|"
    r"(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}|sa_otlp_[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def capture_level(value: str | None) -> str:
    """Validate a level; legacy ``optional`` means full content capture."""
    level = str(value or "structural").strip().lower()
    if level == "optional":
        level = "full"
    return level if level in CAPTURE_LEVELS else "structural"


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """Recursively redact secret-like keys and credential-shaped strings."""
    if key and _SENSITIVE_KEY.search(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact_sensitive(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return _SECRET_VALUE.sub(_REDACTED, value)
    return value


def sanitize_span(span: Mapping[str, Any], level: str | None = None) -> dict[str, Any]:
    """Keep safe span structure and only the content allowed by ``level``."""
    selected_level = capture_level(level)
    safe = redact_sensitive(dict(span))
    structural = {
        key: safe[key]
        for key in (
            "trace_id", "span_id", "name", "kind", "parent_span_id",
            "start_time", "end_time", "status", "attributes",
        )
        if key in safe
    }
    attributes = structural.get("attributes")
    content_keys = {
        "gen_ai.prompt", "gen_ai.completion", "gen_ai.input.messages",
        "gen_ai.output.messages", "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result", "tool.arguments", "tool.result",
        "input.value", "output.value", "llm.input.messages",
        "llm.output.messages",
    }
    if isinstance(attributes, dict):
        structural["attributes"] = {
            key: value for key, value in attributes.items() if key not in content_keys
        }
    if selected_level in {"inputs", "full"}:
        for key in ("arguments", "input", "inputs"):
            if key in safe:
                structural[key] = safe[key]
        if isinstance(attributes, dict):
            structural["attributes"].update({
                key: value for key, value in attributes.items()
                if key in {
                    "gen_ai.prompt", "gen_ai.input.messages",
                    "gen_ai.tool.call.arguments", "tool.arguments",
                    "input.value", "llm.input.messages",
                }
            })
    if selected_level == "full":
        for key in ("result", "output", "outputs", "events"):
            if key in safe:
                structural[key] = safe[key]
        if isinstance(attributes, dict):
            structural["attributes"].update({
                key: value for key, value in attributes.items()
                if key in {
                    "gen_ai.completion", "gen_ai.output.messages",
                    "gen_ai.tool.call.result", "tool.result", "output.value",
                    "llm.output.messages",
                }
            })
    structural["capture_level"] = selected_level
    return structural


def apply_content_capture_level(traces: list[Mapping[str, Any]], level: str) -> list[dict[str, Any]]:
    """Sanitize spans for persistence or judge serialization."""
    return [sanitize_span(trace, level) for trace in traces]


def serialize_for_judge(traces: list[Mapping[str, Any]], level: str | None = None) -> list[dict[str, Any]]:
    """Final defense-in-depth boundary before a judge prompt."""
    return apply_content_capture_level(traces, level or "full")


def contains_secret(value: Any) -> bool:
    """Return whether serialized data still contains a sensitive key."""
    return bool(_SENSITIVE_KEY.search(json.dumps(value, default=str)))
