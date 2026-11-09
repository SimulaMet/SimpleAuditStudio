"""Compact, generic display of knowledge-base retrieval sources.

A retrieval step reports its sources as arbitrary strings: deep web URLs,
bare domains, URLs keyed by query parameters or opaque ids, object-store
paths (``s3://…``), DOIs, file names or plain titles. ``source_chip`` turns
any of them into a short readable label plus an optional safe link, and
``annotate_source_chips`` adds the chips to timeline rows, marking sources
an earlier step already returned.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit

REDACTION_MARK = "[REDACTED]"
LABEL_MAX = 60

_GENERIC_STEMS = {"index", "default", "home", "main", "view", "page", "article", "doc", "document", "content", "show"}
_DOI_RE = re.compile(r"^(?:doi:)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
# A scheme-less web address needs a path or "www." so "refunds.md" stays a file name.
_BARE_HOST_RE = re.compile(r"^(?:www\.[a-z0-9.-]+\.[a-z]{2,}(?:[/?#]\S*)?|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}[/?#]\S*)$", re.IGNORECASE)
_UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
_TITLE_KEYS = ("p", "page", "title", "q", "id", "doc", "docid", "article", "name", "slug")


def _shorten(text: str, limit: int = LABEL_MAX) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _humanize(token: str) -> str:
    """URL-decode a slug, swap separators for spaces, capitalise all-lowercase slugs."""
    text = unquote(token or "").strip()
    if not text:
        return ""
    slug = " " not in text
    text = re.sub(r"[-_+]+", " ", text).strip()
    if slug and text == text.lower():
        text = text[:1].upper() + text[1:]
    return text


def _split_ext(segment: str) -> tuple[str, str]:
    stem, dot, ext = segment.rpartition(".")
    if dot and stem and 1 <= len(ext) <= 5 and ext.isalnum() and not ext.isdigit():
        return stem, ext.upper()
    return segment, ""


def _is_opaque(token: str) -> bool:
    """Machine ids (numbers, hashes, UUIDs, random tokens) that read badly as labels."""
    if token.isdigit() or _UUID_RE.match(token):
        return True
    has_digit = any(ch.isdigit() for ch in token)
    if len(token) >= 8 and has_digit and re.fullmatch(r"[0-9a-fA-F]+", token):
        return True
    return len(token) >= 16 and has_digit and not re.search(r"[-_ ]", token)


def _path_label(segments: list[str]) -> tuple[str, str]:
    """Best label from path segments: (label, extension badge)."""
    ext = ""
    for i in range(len(segments) - 1, -1, -1):
        stem, seg_ext = _split_ext(unquote(segments[i]))
        if i == len(segments) - 1:
            ext = seg_ext
        if stem.lower() in _GENERIC_STEMS:
            continue
        if _is_opaque(stem):
            parent = next(
                (
                    _split_ext(unquote(s))[0]
                    for s in reversed(segments[:i])
                    if not _is_opaque(_split_ext(unquote(s))[0]) and _split_ext(unquote(s))[0].lower() not in _GENERIC_STEMS
                ),
                "",
            )
            short = stem if len(stem) <= 10 else stem[:8] + "…"
            return (f"{_humanize(parent)} · {short}" if parent else short), ext
        return _humanize(stem), ext
    return "", ext


def _query_label(query: str) -> str:
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=False) if v]
    for key in _TITLE_KEYS:
        for k, v in pairs:
            if k.lower() == key:
                return _humanize(v) if not _is_opaque(v) else f"{k}={v[:12]}"
    if pairs:
        k, v = pairs[0]
        return f"{k}={_shorten(v, 16)}"
    return ""


def source_chip(source) -> dict | None:
    """Normalise one retrieval source into a display chip.

    Returns ``{label, sublabel, href, host, full, key, kind, ext, redacted}``
    or ``None`` for an empty source. ``href`` is only set for safe http(s)
    links whose text was not altered by redaction.
    """
    title = url = None
    if isinstance(source, dict):
        title = source.get("title") or source.get("name")
        url = source.get("url") or source.get("source") or ""
        raw = str(url or title or "").strip()
    else:
        raw = str(source if source is not None else "").strip()
        url = raw
    if not raw:
        return None
    redacted = REDACTION_MARK in raw
    chip = {
        "label": "", "sublabel": "", "href": None, "host": "", "full": raw,
        "key": raw.rstrip("/"), "kind": "text", "ext": "", "redacted": redacted,
    }
    text = str(url or "")
    doi = _DOI_RE.match(text)
    if doi:
        chip.update(kind="doi", label=_shorten(doi.group(1)), host="doi.org")
        if not redacted:
            chip["href"] = "https://doi.org/" + doi.group(1)
    else:
        candidate = text
        if "://" not in candidate and _BARE_HOST_RE.match(candidate) and " " not in candidate:
            candidate = "https://" + candidate
        parts = urlsplit(candidate) if "://" in candidate or candidate.startswith("file:") else None
        if parts and parts.scheme:
            scheme = parts.scheme.lower()
            host = (parts.hostname or "").lower()
            segments = [s for s in parts.path.split("/") if s]
            label, ext = _path_label(segments)
            sub = _humanize(parts.fragment) if parts.fragment else ""
            q_label = _query_label(parts.query)
            if not label:
                label, sub = (q_label, sub) if q_label else (host or scheme, sub)
            elif q_label and not sub:
                sub = q_label
            chip.update(
                label=_shorten(label), sublabel=_shorten(sub, 40), ext=ext,
                kind="web" if scheme in ("http", "https") else "uri",
                host=host if scheme in ("http", "https") else scheme,
                key=f"{scheme}://{host}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else ""),
            )
            if scheme in ("http", "https") and host and not redacted:
                chip["href"] = candidate
        else:
            stem, ext = _split_ext(text.rsplit("/", 1)[-1]) if "/" in text or "." in text else (text, "")
            label = _humanize(stem) if (ext or "/" in text) else text
            chip.update(label=_shorten(label or text), ext=ext, kind="file" if ext else "text")
    if title:
        # A real title says more than the URL path; the URL stays in the tooltip.
        chip.update(label=_shorten(str(title)), sublabel="")
    chip["title"] = str(title) if title else ""
    chip["doc_id"] = str(source.get("id") or "") if isinstance(source, dict) else ""
    chip["score"] = None
    if isinstance(source, dict) and source.get("score") is not None:
        try:
            chip["score"] = round(float(source["score"]), 2)
        except (TypeError, ValueError):
            pass
    return chip


def _tooltip(chip: dict) -> str:
    """Multi-line hover text: everything known about the source, nothing truncated."""
    lines = [chip["title"]] if chip["title"] else []
    if chip["score"] is not None:
        lines.append(f"Score: {chip['score']}")
    if chip["doc_id"]:
        lines.append(f"ID: {chip['doc_id']}")
    if chip["full"] != chip["title"]:
        lines.append(chip["full"])
    if chip["seen_in"]:
        lines.append(f"Also returned by {chip['seen_in']}")
    if chip["redacted"]:
        lines.append("Redacted: link disabled")
    return "\n".join(lines)


def annotate_source_chips(rows: list[dict], *, visible: int = 3) -> None:
    """Add ``kb_chips``, ``kb_hosts``, ``kb_hidden`` to each row with ``kb_sources``.

    A chip whose source an earlier row already returned gets ``seen_in`` set
    to that row's label, so repeated retrievals read as repeats.
    """
    first_seen: dict[str, str] = {}
    for row in rows:
        chips = []
        keys = set()
        for src in row.get("kb_docs") or row.get("kb_sources") or []:
            chip = source_chip(src)
            if not chip or chip["key"] in keys:
                continue
            keys.add(chip["key"])
            chip["seen_in"] = first_seen.get(chip["key"], "")
            chip["tooltip"] = _tooltip(chip)
            chips.append(chip)
        for chip in chips:
            first_seen.setdefault(chip["key"], str(row.get("label") or row.get("kind") or "an earlier step"))
        hosts = list(dict.fromkeys(c["host"] for c in chips if c["host"]))
        row["kb_chips"] = chips
        row["kb_visible"] = visible
        row["kb_hidden"] = max(0, len(chips) - visible)
        row["kb_hosts"] = hosts[:2]
        row["kb_hosts_more"] = max(0, len(hosts) - 2)
