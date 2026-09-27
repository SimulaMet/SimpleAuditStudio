"""Transient database locks are retried, not surfaced as failures."""
from unittest import mock

from django.db import OperationalError, transaction
from django.test import SimpleTestCase, TestCase

from infra import db


class RetryIfLockedTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(db.time, "sleep")   # no real waiting in tests
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def _flaky(self, failures, error="database is locked"):
        calls = {"n": 0}

        @db.retry_if_locked
        def write():
            calls["n"] += 1
            if calls["n"] <= failures:
                raise OperationalError(error)
            return "ok"

        return write, calls

    def test_retries_until_the_lock_clears(self):
        write, calls = self._flaky(3)
        self.assertEqual(write(), "ok")
        self.assertEqual(calls["n"], 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_gives_up_after_the_budget(self):
        write, calls = self._flaky(100)
        with self.assertRaises(OperationalError):
            write()
        self.assertEqual(calls["n"], db.ATTEMPTS)

    def test_other_errors_are_not_retried(self):
        write, calls = self._flaky(1, error="no such table: core_audit_run")
        with self.assertRaises(OperationalError):
            write()
        self.assertEqual(calls["n"], 1)

    def test_postgres_deadlock_is_transient(self):
        self.assertTrue(db.is_transient_db_error(OperationalError("deadlock detected")))


class RetryInsideTransactionTests(TestCase):
    def test_inner_write_reraises_so_the_outer_unit_retries(self):
        calls = {"n": 0}

        @db.retry_if_locked
        def write():
            calls["n"] += 1
            raise OperationalError("database is locked")

        with mock.patch.object(db.time, "sleep"), self.assertRaises(OperationalError), transaction.atomic():
            write()
        self.assertEqual(calls["n"], 1)


class FreshConnectionTests(SimpleTestCase):
    def test_closes_stale_connections_around_the_call(self):
        with mock.patch.object(db, "close_old_connections") as close:
            self.assertEqual(db.with_fresh_connection(lambda: 42)(), 42)
        self.assertEqual(close.call_count, 2)
