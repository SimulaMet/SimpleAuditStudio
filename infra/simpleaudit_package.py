"""Resolve SimpleAudit engine provenance from the installed package.

The SimpleAudit engine is a normal pip dependency of this project (declared in
``pyproject.toml``). Its provenance — the version stamped into every frozen
AuditRun manifest and the optional git commit — is read from the *installed*
package metadata, never configured by hand:

- ``version`` comes from ``importlib.metadata.version("simpleaudit")`` (the
  ``Version`` field of the distribution metadata).
- ``commit`` comes from the PEP 610 ``direct_url.json`` record when the package
  was installed from a VCS source (e.g. ``git+https://...@<ref>``). When it was
  installed from a registry wheel/sdist there is no commit, so this is ``None``.

This module is deliberately import-safe: importing it never imports the engine
and never raises. Callers get ``None`` for fields they cannot determine, which
keeps web-only and test environments (where the engine may not be installed)
working.

Development override
--------------------
For local development against an unmerged engine revision, a developer may drop
a ``simpleaudit-dependency.yaml`` at the project root containing ONLY a
``git_ref:`` key::

    git_ref: d19785f0d9d3bd9cb647b519c08661cbab0c4d60

That value is recorded as the run's ``git_commit`` provenance instead of the
installed package's commit. It is a dev convenience only — production relies on
the committed ``pyproject.toml`` pin, and the file is gitignored so it can
never leak into a deployment. There is intentionally no ``path:`` override: the
engine is always consumed as an installed package.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Project root = parent of the ``infra`` package directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OVERRIDE_FILE = _PROJECT_ROOT / "simpleaudit-dependency.yaml"


@dataclass(frozen=True)
class EngineProvenance:
    """Resolved SimpleAudit engine provenance.

    ``version`` is authoritative and required for production runs. ``commit``
    is optional provenance: present when the package was installed from a VCS
    ref (or a dev override supplies one), absent for registry installs.
    """

    version: str | None
    commit: str | None
    source: str  # "metadata" | "dev_override" | "unavailable"


def _read_direct_url_commit() -> str | None:
    """Return the VCS commit id from PEP 610 ``direct_url.json``, or ``None``."""
    try:
        import importlib.metadata as md

        raw = md.distribution("simpleaudit").read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - missing dist/file is simply "no commit"
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    vcs_info = data.get("vcs_info") or {}
    commit = vcs_info.get("commit_id")
    return commit if isinstance(commit, str) and commit else None


def _read_dev_override_commit() -> str | None:
    """Return the ``git_ref`` from the optional dev override file, or ``None``.

    The file is parsed with PyYAML when available; otherwise a minimal
    ``key: value`` scan is used so the override works without the dependency.
    Only a non-empty ``git_ref`` string is honoured.
    """
    if not _OVERRIDE_FILE.is_file():
        return None
    try:
        text = _OVERRIDE_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    data: dict | None = None
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:  # noqa: BLE001 - fall back to the naive parser below
        data = None
    if data is None:
        data = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            data[key.strip()] = value.strip().strip("'\"")
    ref = data.get("git_ref")
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return None


@lru_cache(maxsize=1)
def resolve_engine_provenance() -> EngineProvenance:
    """Resolve the installed SimpleAudit engine's provenance (cached).

    Resolution order for the commit:
      1. dev override ``git_ref`` (if the file exists) — recorded as-is;
      2. PEP 610 ``direct_url.json`` commit (VCS installs);
      3. ``None`` (registry installs have no commit).

    The version always comes from installed package metadata. If the package is
    not installed at all, both are ``None`` and ``source`` is ``"unavailable"``.
    """
    try:
        import importlib.metadata as md

        version = md.version("simpleaudit")
    except Exception:  # noqa: BLE001 - PackageNotFoundError or malformed metadata
        return EngineProvenance(version=None, commit=None, source="unavailable")

    override_commit = _read_dev_override_commit()
    if override_commit:
        return EngineProvenance(version=version, commit=override_commit, source="dev_override")

    direct_url_commit = _read_direct_url_commit()
    if direct_url_commit:
        return EngineProvenance(version=version, commit=direct_url_commit, source="metadata")

    return EngineProvenance(version=version, commit=None, source="metadata")


def clear_cache() -> None:
    """Clear the resolution cache (used by tests after mutating the environment)."""
    resolve_engine_provenance.cache_clear()
