"""Operator-defined extensions: menu items, CSS/JS slots, embed pages (issue #83).

``SIMPLEAUDIT_EXTENSIONS`` holds a JSON manifest inline, or the path of a JSON
file. With the setting unset nothing here does anything: no context, no markup.

The manifest is **operator-controlled code**, not user input. Its scripts run
with full same-origin privileges, so it is read only from the deployment's
configuration and never from the database. See ``docs/extensions.md``.

Roles form a ladder: ``user < viewer < auditor < admin < superuser``. A user's
level is the highest role they hold in any workspace (any signed-in user is at
least ``user``; a superuser is ``superuser``). An entry is shown to users at or
above the lowest role it lists; the default is ``admin``.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("simpleaudit.extensions")

ROLE_LADDER = ("user", "viewer", "auditor", "admin", "superuser")
_LEVEL = {role: i for i, role in enumerate(ROLE_LADDER)}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_SRI_RE = re.compile(r"^(sha256|sha384|sha512)-[A-Za-z0-9+/=]+$")


@dataclass(frozen=True)
class Extension:
    slug: str
    label: str
    icon: str = "◇"
    section: str = ""
    order: int = 100
    min_level: int = _LEVEL["admin"]
    href: str = ""
    embed: str = ""
    css: tuple[str, ...] = ()
    js: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    integrity: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        """Where the menu item points: the embed page, the href, or nothing."""
        if self.embed:
            return f"/x/{self.slug}/"
        return self.href

    def applies_to(self, path: str) -> bool:
        """Whether this entry's CSS/JS load on ``path`` (no ``pages`` = every page)."""
        return not self.pages or any(fnmatch.fnmatchcase(path, p) for p in self.pages)


def _is_url(value: str, *, internal_only: bool = False) -> bool:
    """An absolute http(s) URL, or a same-origin path (``/x``, never ``//host``)."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        return False
    if value.startswith("/") and not value.startswith("//"):
        return True
    return not internal_only and bool(re.match(r"^https?://[^/]+", value, re.IGNORECASE))


def _str_list(entry: dict, key: str, slug: str) -> tuple[str, ...]:
    raw = entry.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(v, str) and v for v in raw):
        raise ImproperlyConfigured(f"Extension {slug!r}: {key!r} must be a string or a list of strings.")
    return tuple(raw)


def parse_entry(entry: dict) -> Extension:
    """Validate one manifest entry; raises ImproperlyConfigured with the reason."""
    if not isinstance(entry, dict):
        raise ImproperlyConfigured("Each extension must be a JSON object.")
    slug = entry.get("slug")
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ImproperlyConfigured(f"Extension slug {slug!r} must be lowercase letters, digits and dashes.")
    roles = _str_list(entry, "roles", slug) or ("admin",)
    unknown = [r for r in roles if r not in _LEVEL]
    if unknown:
        raise ImproperlyConfigured(f"Extension {slug!r}: unknown role(s) {unknown}; use {list(ROLE_LADDER)}.")
    href, embed = entry.get("href", ""), entry.get("embed", "")
    if href and embed:
        raise ImproperlyConfigured(f"Extension {slug!r}: give 'href' or 'embed', not both.")
    if href and not _is_url(href):
        raise ImproperlyConfigured(f"Extension {slug!r}: 'href' must be an http(s) URL or a path starting with '/'.")
    if embed and not _is_url(embed, internal_only=True):
        raise ImproperlyConfigured(f"Extension {slug!r}: 'embed' must be a same-origin path starting with '/'.")
    css, js = _str_list(entry, "css", slug), _str_list(entry, "js", slug)
    for url in css + js:
        if not _is_url(url):
            raise ImproperlyConfigured(f"Extension {slug!r}: asset {url!r} must be an http(s) URL or a '/' path.")
    integrity = entry.get("integrity") or {}
    if not isinstance(integrity, dict) or not all(
        isinstance(v, str) and all(_SRI_RE.match(h) for h in v.split()) for v in integrity.values()
    ):
        raise ImproperlyConfigured(f"Extension {slug!r}: 'integrity' must map asset URLs to sha256/384/512 hashes.")
    order = entry.get("order", 100)
    if not isinstance(order, int) or isinstance(order, bool):
        raise ImproperlyConfigured(f"Extension {slug!r}: 'order' must be an integer.")
    return Extension(
        slug=slug,
        label=str(entry.get("label") or slug),
        icon=str(entry.get("icon") or "◇"),
        section=str(entry.get("section") or ""),
        order=order,
        min_level=min(_LEVEL[r] for r in roles),
        href=href,
        embed=embed,
        css=css,
        js=js,
        pages=_str_list(entry, "pages", slug),
        integrity=dict(integrity),
    )


def parse_manifest(text: str) -> tuple[Extension, ...]:
    """Parse manifest JSON: a list of entries, or ``{"extensions": [...]}``."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured(f"SIMPLEAUDIT_EXTENSIONS is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("extensions")
    if not isinstance(data, list):
        raise ImproperlyConfigured("SIMPLEAUDIT_EXTENSIONS must be a JSON list (or {\"extensions\": [...]}).")
    entries = tuple(parse_entry(e) for e in data)
    slugs = [e.slug for e in entries]
    dupes = sorted({s for s in slugs if slugs.count(s) > 1})
    if dupes:
        raise ImproperlyConfigured(f"Duplicate extension slug(s): {dupes}.")
    return tuple(sorted(entries, key=lambda e: (e.section, e.order, e.label.lower())))


# Cache keyed by (setting value, file mtime) so tests and file edits take effect
# without a restart, while ordinary requests do one stat() at most.
_cache: dict[tuple, tuple[Extension, ...]] = {}


def load_extensions() -> tuple[Extension, ...]:
    """The configured extensions (empty when unset). A bad manifest is logged and ignored."""
    raw = (getattr(settings, "SIMPLEAUDIT_EXTENSIONS", "") or "").strip()
    if not raw:
        return ()
    is_inline = raw.startswith(("[", "{"))
    mtime = None
    if not is_inline:
        try:
            mtime = os.stat(raw).st_mtime_ns
        except OSError:
            logger.error("SIMPLEAUDIT_EXTENSIONS file %s is not readable; extensions disabled.", raw)
            return ()
    key = (raw, mtime)
    if key not in _cache:
        try:
            text = raw if is_inline else Path(raw).read_text(encoding="utf-8")
            _cache.clear()
            _cache[key] = parse_manifest(text)
        except (ImproperlyConfigured, OSError, UnicodeDecodeError) as exc:
            # Never take the whole UI down over an operator typo: log loudly, run without.
            logger.error("Invalid SIMPLEAUDIT_EXTENSIONS, extensions disabled: %s", exc)
            _cache.clear()
            _cache[key] = ()
    return _cache[key]


def user_level(user) -> int | None:
    """The user's place on ROLE_LADDER, or None when not signed in."""
    if user is None or not user.is_authenticated:
        return None
    if user.is_superuser:
        return _LEVEL["superuser"]
    cached = getattr(user, "_studio_ext_level", None)   # one query per request
    if cached is not None:
        return cached
    from accounts.models import ProjectMembership

    roles = set(ProjectMembership.objects.filter(user=user).values_list("role", flat=True))
    level = max((_LEVEL[r] for r in roles if r in _LEVEL), default=_LEVEL["user"])
    user._studio_ext_level = level
    return level


def role_level(role: str) -> int | None:
    """Ladder level of a role name, or None if it is not one."""
    return _LEVEL.get(role)


def visible_extensions(user) -> list[Extension]:
    """Extensions this user may see (empty list for anonymous users)."""
    extensions = load_extensions()
    if not extensions:
        return []
    level = user_level(user)
    if level is None:
        return []
    return [e for e in extensions if level >= e.min_level]


def get_extension(slug: str, user) -> Extension | None:
    """The visible extension with this slug, or None (unknown and forbidden look the same)."""
    return next((e for e in visible_extensions(user) if e.slug == slug), None)
