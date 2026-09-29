"""Local installs keep their data outside the installed package.

Regression: the database lived in site-packages, so every new version started
empty and ``uv cache clean`` deleted it.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_data_dir
"""
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from simpleaudit_studio import paths


def _studio_db(path: Path, runs: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE core_audit_run (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO core_audit_run DEFAULT VALUES", [()] * runs)
    return path


class DataDirTests(SimpleTestCase):
    def test_run_table_name_matches_the_model(self):
        from audits.models import AuditRun

        self.assertEqual(AuditRun._meta.db_table, "core_audit_run")   # paths._run_count reads it

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"SIMPLEAUDIT_DATA_DIR": str(self.tmp / "data"),
                                                "UV_CACHE_DIR": str(self.tmp / "uv")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_database_lives_in_the_data_folder(self):
        self.assertEqual(paths.database_path(), self.tmp / "data" / "studio.sqlite3")
        with mock.patch.dict(os.environ, {"SIMPLEAUDIT_DATA_DIR": ""}):
            self.assertEqual(paths.data_dir(), Path("~/.simpleaudit-studio").expanduser())

    def test_old_uvx_installs_are_found(self):
        old = _studio_db(self.tmp / "uv/archive-v0/abc/lib/python3.11/site-packages/demo.sqlite3", 2)
        self.assertIn(old, paths.legacy_databases())

    def test_newest_studio_database_is_adopted_once(self):
        older = _studio_db(self.tmp / "a/demo.sqlite3", 1)
        time.sleep(0.02)
        newer = _studio_db(self.tmp / "b/demo.sqlite3", 5)
        time.sleep(0.02)
        other = self.tmp / "c/demo.sqlite3"
        other.parent.mkdir()
        sqlite3.connect(other).execute("CREATE TABLE unrelated (x)").connection.close()   # newest, not Studio
        with mock.patch.object(paths, "legacy_databases", return_value=[older, other, newer]):
            self.assertEqual(paths.adopt_legacy_database(), (newer, 5))
            with sqlite3.connect(paths.database_path()) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM core_audit_run").fetchone()[0], 5)
            self.assertIsNone(paths.adopt_legacy_database())   # the data folder has one now
        self.assertTrue(newer.exists())   # the old copy is left alone

    def test_nothing_to_adopt(self):
        with mock.patch.object(paths, "legacy_databases", return_value=[]):
            self.assertIsNone(paths.adopt_legacy_database())
        self.assertFalse(paths.database_path().exists())

    def test_uncommitted_wal_rows_come_along(self):
        src = _studio_db(self.tmp / "w/demo.sqlite3", 1)
        live = sqlite3.connect(src)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO core_audit_run DEFAULT VALUES")
        live.commit()   # in the WAL, not yet checkpointed into the main file
        try:
            with mock.patch.object(paths, "legacy_databases", return_value=[src]):
                self.assertEqual(paths.adopt_legacy_database()[1], 2)
        finally:
            live.close()
