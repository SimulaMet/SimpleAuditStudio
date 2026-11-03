"""Uniform, presentation-safe view of the *frozen* target metadata dict."""
from __future__ import annotations

import json


def flattened_metadata(metadata: dict | None) -> dict[str, str]:
    """Flatten dictionaries into dot paths; arrays remain one JSON-valued row.

    Every field, including false, zero, null, and empty containers, is retained.
    Values are strings so the run detail and comparison use identical labels.
    """
    if not isinstance(metadata, dict):
        return {}
    out: dict[str, str] = {}

    def visit(prefix: str, value) -> None:
        if isinstance(value, dict) and value:
            for key in sorted(value):
                visit(f"{prefix}.{key}" if prefix else str(key), value[key])
        elif isinstance(value, str):
            out[prefix] = value
        else:
            out[prefix] = json.dumps(value, ensure_ascii=False, sort_keys=True)

    for key in sorted(metadata):
        visit(str(key), metadata[key])
    return out


def frozen_target_metadata(run) -> dict[str, str]:
    snapshot = run.target_config_snapshot or {}
    return flattened_metadata(snapshot.get("remote_metadata"))


#: Flags that are noise on the run page when off. Stored in full; only hidden on display.
_HIDE_WHEN_OFF_KEYS = {"background_image_url"}
_HIDE_WHEN_OFF_PREFIXES = ("builtinTools.", "capabilities.")
_OFF_VALUES = {"false", "null", "", "0"}


def _hidden_when_off(key: str, value: str) -> bool:
    if key not in _HIDE_WHEN_OFF_KEYS and not key.startswith(_HIDE_WHEN_OFF_PREFIXES):
        return False
    return value in _OFF_VALUES


def metadata_rows(run) -> list[dict]:
    return [
        {"key": key, "value": value, "long": len(value) > 160}
        for key, value in frozen_target_metadata(run).items()
        if not _hidden_when_off(key, value)
    ]
