"""Privacy-safe trace capture and serialization helpers."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

CAPTURE_LEVELS = {"structural", "inputs", "full"}
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|auth[_-]?header|cookie|set-cookie|"
    r"password|passwd|secret|credential|private[_-]?key|access[_-]?key|"
    # A token segment only: "access_token" matches, "gen_ai.usage.input_tokens" does not.
    r"(?:^|[_.-])token(?:$|[_.-]))",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    # A bearer/basic credential is a long opaque string; plain words after
    # "basic" ("basic refund question") are not secrets.
    r"(?:bearer\s+|basic\s+)(?=[A-Za-z0-9+/=._~-]*[0-9+/=._~-])[A-Za-z0-9+/=._~-]{16,}|"
    r"(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}|sa_otlp_[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"

# Content kept at every level: the tool arguments and results the checks read.
# Capture levels, by what each keeps (the default is ``structural``):
#   structural  skeleton, tool arguments/results, retrieval sources, prompts,
#               and completions: everything the audit checks and judge read.
#   inputs      structural without model output (completions, output values).
#   full        everything, including span events and raw result/output fields.
_OUTPUT_KEYS = frozenset({
    "gen_ai.completion", "gen_ai.output.messages", "llm.output.messages", "output.value",
})
_OUTPUT_PREFIXES = ("llm.output_messages.",)


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


def _keep_attribute(key: str, level: str) -> bool:
    """Whether an attribute survives capture at ``level``."""
    return not (level == "inputs" and (key in _OUTPUT_KEYS or key.startswith(_OUTPUT_PREFIXES)))


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
    if isinstance(attributes, dict):
        structural["attributes"] = {
            key: value for key, value in attributes.items() if _keep_attribute(key, selected_level)
        }
    for key in ("arguments", "input", "inputs"):
        if key in safe:
            structural[key] = safe[key]
    if selected_level == "full":
        for key in ("result", "output", "outputs", "events"):
            if key in safe:
                structural[key] = safe[key]
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
