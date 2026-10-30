"""Forbidden tool entries as typed in the scenario builder.

One entry is a tool name, or a tool name with argument values that make a call
forbidden: ``lookup`` or ``lookup(id=42, mode="full")``. Entries are separated
by commas outside parentheses. Argument values are JSON literals when they parse
(numbers, booleans, null, quoted strings); otherwise they are plain strings.
"""
from __future__ import annotations

import json
import re
from typing import Any

_ENTRY = re.compile(r"^([A-Za-z0-9_.:-]+)\s*(?:\((.*)\))?$", re.DOTALL)


def _split_top_level(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _value(raw: str) -> Any:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_tool_entries(text: str) -> list[Any]:
    """Builder text to the stored form: names as strings, argument entries as objects."""
    entries: list[Any] = []
    for part in _split_top_level(text or ""):
        match = _ENTRY.match(part)
        if not match:
            raise ValueError(f"Invalid forbidden tool entry: {part!r}")
        name, args_text = match.group(1), match.group(2)
        if args_text is None or not args_text.strip():
            entries.append(name)
            continue
        arguments: dict[str, Any] = {}
        for pair in args_text.split(","):
            if "=" not in pair:
                raise ValueError(f"Argument must be key=value in {part!r}")
            key, _, value = pair.partition("=")
            if not key.strip():
                raise ValueError(f"Argument name is empty in {part!r}")
            arguments[key.strip()] = _value(value)
        entries.append({"name": name, "arguments": arguments})
    return entries


def format_tool_entries(entries: Any) -> str:
    """Stored entries back to builder text."""
    if not isinstance(entries, list):
        return ""
    rendered = []
    for entry in entries:
        if isinstance(entry, str):
            rendered.append(entry)
        elif isinstance(entry, dict) and entry.get("name"):
            args = entry.get("arguments") or {}
            pairs = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
            rendered.append(f"{entry['name']}({pairs})" if args else entry["name"])
    return ", ".join(rendered)
