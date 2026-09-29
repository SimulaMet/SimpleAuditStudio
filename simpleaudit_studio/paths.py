"""Where a local install keeps its data.

``uvx simpleaudit-studio`` (and the single-container image) store everything
in one folder outside the installed package, so upgrading, reinstalling or
``uv cache clean`` never touches it:

    ~/.simpleaudit-studio/            (SIMPLEAUDIT_DATA_DIR overrides)
        studio.sqlite3                the database: runs, scenarios, judges, ...
        embedded-pg/                  the embedded Hatchet engine's queue

Versions before 0.5.2 kept the database inside the installed package
(``site-packages/demo.sqlite3``), so each new version started empty.
``adopt_legacy_database`` copies the newest of those into the data folder once.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_NAME = "studio.sqlite3"
LEGACY_DB_NAME = "demo.sqlite3"


def data_dir() -> Path:
    """The data folder (not created)."""
    return Path(os.environ.get("SIMPLEAUDIT_DATA_DIR") or "~/.simpleaudit-studio").expanduser()


def database_path() -> Path:
    return data_dir() / DB_NAME


def _uv_cache_dirs() -> list[Path]:
    dirs = [os.environ.get("UV_CACHE_DIR"), "~/.cache/uv", "~/Library/Caches/uv"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(os.path.join(os.environ["LOCALAPPDATA"], "uv", "cache"))
    return [Path(d).expanduser() for d in dirs if d]


def legacy_databases() -> list[Path]:
    """Databases earlier versions left inside installed packages: this
    install's own, and every uvx install in the uv cache."""
    package_root = Path(__file__).resolve().parent.parent   # site-packages (or the repo)
    found = {package_root / LEGACY_DB_NAME}
    for cache in _uv_cache_dirs():
        for pattern in ("archive-v0/*/lib/python*/site-packages/" + LEGACY_DB_NAME,
                        "archive-v0/*/Lib/site-packages/" + LEGACY_DB_NAME):
            found.update(cache.glob(pattern))
    return [p for p in found if p.is_file() and p.stat().st_size > 0]


def _last_used(path: Path) -> float:
    wal = path.with_name(path.name + "-wal")
    return max(path.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0)


def _run_count(path: Path) -> int | None:
    """Audit runs in a Studio database; None when it isn't one."""
    try:
        with sqlite3.connect(path) as conn:
            # AuditRun's table (audits.models; this runs before Django is set up).
            return conn.execute("SELECT COUNT(*) FROM core_audit_run").fetchone()[0]
    except sqlite3.Error:
        return None


def adopt_legacy_database() -> tuple[Path, int] | None:
    """Copy the most recently used pre-0.5.2 database into the data folder, when
    the data folder has none yet. Returns (source, run count), or None."""
    target = database_path()
    if target.exists():
        return None
    candidates = [(p, n) for p in sorted(legacy_databases(), key=_last_used, reverse=True)
                  if (n := _run_count(p)) is not None]
    if not candidates:
        return None
    source, runs = candidates[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    # SQLite's backup API: a consistent copy, WAL contents included.
    with sqlite3.connect(source) as src, sqlite3.connect(partial) as dst:
        src.backup(dst)
    partial.replace(target)
    return source, runs
