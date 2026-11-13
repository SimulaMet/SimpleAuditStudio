"""Expected-behaviour entries: plain strings or objects with a ``required`` flag.

A scenario's ``expected_behavior`` (and a set version's ``shared_expectations``)
is a JSON list whose entries are either a plain string or an object::

    {"id": "G1", "text": "...", "required": false}

Only ``text`` is required. A plain string means a required item. ``required``
decides whether a violation can change the derived severity; a non-required
item is graded and reported but never counts. The flag is applied by the
SimpleAudit engine in code and is never shown to the judge model.

This module validates and stores entries without importing the engine, so the
web process can check a form before anything runs. The engine's own
``simpleaudit.expectations`` module (reached only through ``infra/engine.py``)
generates missing ids and merges shared rules at execution time.

Entries are stored in their smallest form: a required item without an id is
kept as a plain string, so scenarios that use no flags keep the exact shape,
and content hash, they had before expectation objects existed.
"""
from __future__ import annotations

from typing import Any

MAX_ID_LENGTH = 64


def _entry(raw: Any) -> str | dict:
    if isinstance(raw, str):
        text, required, item_id = raw, True, None
    elif isinstance(raw, dict):
        text = raw.get("text")
        required = raw.get("required", True)
        item_id = raw.get("id")
        if not isinstance(required, bool):
            raise ValueError(f"'required' must be true or false, got {required!r}")  # noqa: TRY004 - callers report one ValueError type
        unknown = set(raw) - {"id", "text", "required", "source"}
        if unknown:
            raise ValueError(f"unknown expectation field(s): {', '.join(sorted(unknown))}")
    else:
        raise ValueError(f"an expectation must be a string or an object with 'text', got {type(raw).__name__}")  # noqa: TRY004 - callers report one ValueError type
    if not isinstance(text, str) or not text.strip():
        raise ValueError("an expectation needs a non-empty 'text'")
    text = text.strip()
    if item_id is not None:
        item_id = str(item_id).strip()
        if len(item_id) > MAX_ID_LENGTH:
            raise ValueError(f"expectation id is longer than {MAX_ID_LENGTH} characters")
    if required and not item_id:
        return text
    entry: dict[str, Any] = {}
    if item_id:
        entry["id"] = item_id
    entry["text"] = text
    entry["required"] = required
    return entry


def clean_expectations(entries: Any) -> list[str | dict]:
    """Validated entries in their stored form (see the module docstring).

    ``source`` is dropped: it is decided by where an entry is stored (a
    scenario's own list or a set's shared rules), not by the entry. Explicit
    ids must be unique. Raises ``ValueError`` with a readable message.
    """
    if entries in (None, ""):
        return []
    if isinstance(entries, (str, dict)):
        entries = [entries]
    if not isinstance(entries, list):
        raise ValueError("expected behaviour must be a list")  # noqa: TRY004 - callers report one ValueError type
    cleaned = [_entry(raw) for raw in entries]
    seen: set[str] = set()
    for entry in cleaned:
        item_id = entry.get("id") if isinstance(entry, dict) else None
        if item_id:
            if item_id in seen:
                raise ValueError(f"duplicate expectation id {item_id!r}")
            seen.add(item_id)
    return cleaned


def expectation_rows(entries: Any) -> list[dict[str, Any]]:
    """Entries as ``{"id", "text", "required"}`` rows for display and editing.

    Never raises: malformed legacy data is shown as text rather than hidden.
    """
    rows = []
    for raw in entries or []:
        if isinstance(raw, dict):
            rows.append({
                "id": str(raw.get("id") or ""),
                "text": str(raw.get("text") or ""),
                "required": raw.get("required", True) is not False,
            })
        else:
            rows.append({"id": "", "text": str(raw), "required": True})
    return rows


def expectation_texts(entries: Any) -> list[str]:
    """Just the texts, in order (what the judge model sees)."""
    return [row["text"] for row in expectation_rows(entries)]


def parse_expectation_form(rows_json: str | None, lines: str | None) -> list[str | dict]:
    """Expected behaviour from a form: the row editor's JSON, else one item per line.

    The row editor posts ``[{"id", "text", "required"}, ...]``; a client
    without JavaScript posts only the plain textarea, where every line is a
    required item. Empty rows are dropped. Raises ``ValueError``.
    """
    import json

    if rows_json and rows_json.strip():
        try:
            rows = json.loads(rows_json)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("expected behaviour rows are not valid JSON") from exc
        if not isinstance(rows, list):
            raise ValueError("expected behaviour rows must be a list")
        rows = [
            row for row in rows
            if not (isinstance(row, dict) and not str(row.get("text") or "").strip())
        ]
        return clean_expectations(rows)
    return [line.strip() for line in (lines or "").splitlines() if line.strip()]


def expectation_display(entries: Any) -> list[str]:
    """One line per entry for diffs: ``[id] text (not counted)``."""
    lines = []
    for row in expectation_rows(entries):
        line = f"[{row['id']}] {row['text']}" if row["id"] else row["text"]
        lines.append(line if row["required"] else f"{line} (not counted)")
    return lines
