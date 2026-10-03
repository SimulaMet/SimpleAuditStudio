"""Unit tests for config.settings.local_sqlite_path().

Plain ``unittest`` (no DB) so they run under both pytest and
``manage.py test``. Each test scrubs ``SIMPLEAUDIT_SQLITE_PATH`` so a leftover
real environment cannot leak in.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from config import settings


class LocalSqlitePathTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("SIMPLEAUDIT_SQLITE_PATH")
        os.environ.pop("SIMPLEAUDIT_SQLITE_PATH", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SIMPLEAUDIT_SQLITE_PATH", None)
        else:
            os.environ["SIMPLEAUDIT_SQLITE_PATH"] = self._saved

    def test_default_is_dev_sqlite3(self):
        self.assertEqual(
            settings.local_sqlite_path(),
            settings.BASE_DIR / "dev.sqlite3",
        )

    def test_blank_is_treated_as_unset(self):
        os.environ["SIMPLEAUDIT_SQLITE_PATH"] = "   "
        self.assertEqual(
            settings.local_sqlite_path(),
            settings.BASE_DIR / "dev.sqlite3",
        )

    def test_relative_resolves_from_repo_root(self):
        os.environ["SIMPLEAUDIT_SQLITE_PATH"] = "data/local.sqlite3"
        self.assertEqual(
            settings.local_sqlite_path(),
            settings.BASE_DIR / "data" / "local.sqlite3",
        )

    def test_absolute_used_as_is(self):
        absolute = Path("/tmp/simpleaudit-test-override.sqlite3")
        os.environ["SIMPLEAUDIT_SQLITE_PATH"] = str(absolute)
        self.assertEqual(settings.local_sqlite_path(), absolute)

    def test_surrounding_whitespace_is_stripped(self):
        absolute = Path("/tmp/simpleaudit-test-override.sqlite3")
        os.environ["SIMPLEAUDIT_SQLITE_PATH"] = f"  {absolute}  "
        self.assertEqual(settings.local_sqlite_path(), absolute)


if __name__ == "__main__":
    unittest.main()
