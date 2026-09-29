"""Connections change in Studio, chat follows — without blocking the save.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test chat.tests.test_sync
"""
import time
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from chat import sync
from chat.api import ChatAPIError
from infra.tests.factories import (
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)


class ConnectionsToPushTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()

    def test_only_enabled_connections_with_a_base_url(self):
        ModelConnectionFactory(project=self.project, name="on", base_url="https://a.example/v1")
        ModelConnectionFactory(project=self.project, name="off", base_url="https://b.example/v1",
                               enabled=False)
        ModelConnectionFactory(project=self.project, name="blank", base_url="")
        self.assertEqual([c["name"] for c in sync.connections_to_push()], ["on"])

    def test_registered_models_narrow_the_connection(self):
        conn = ModelConnectionFactory(project=self.project, base_url="https://a.example/v1")
        RegisteredModelFactory(connection=conn, project=self.project, model_id="gpt-4o")
        RegisteredModelFactory(connection=conn, project=self.project, model_id="gpt-4o-mini")
        RegisteredModelFactory(connection=conn, project=self.project, model_id="gone", enabled=False)
        self.assertEqual(sync.connections_to_push()[0]["model_ids"], ["gpt-4o", "gpt-4o-mini"])

    def test_a_connection_without_registered_models_is_not_narrowed(self):
        ModelConnectionFactory(project=self.project, base_url="https://a.example/v1")
        self.assertEqual(sync.connections_to_push()[0]["model_ids"], [])


class SchedulePushTests(TestCase):
    """The debounce: many changes, one push, off the caller's thread."""

    def setUp(self):
        self.pushes = []
        patcher = patch.object(sync, "push_now", lambda: self.pushes.append(1) or {"pushed": 1, "kept": 0})
        patcher.start()
        self.addCleanup(patcher.stop)
        delay = patch.object(sync, "DELAY_SECONDS", 0.05)
        delay.start()
        self.addCleanup(delay.stop)

    def _settle(self):
        time.sleep(0.3)

    @patch("chat.config.ENABLED", True)
    def test_a_burst_of_changes_is_one_push(self):
        for _ in range(5):
            sync.schedule_push("test")
        self._settle()
        self.assertEqual(len(self.pushes), 1)

    @patch("chat.config.ENABLED", False)
    def test_nothing_is_pushed_while_chat_is_disabled(self):
        sync.schedule_push("test")
        self._settle()
        self.assertEqual(self.pushes, [])

    @patch("chat.config.ENABLED", True)
    def test_a_chat_that_is_not_answering_does_not_raise(self):
        with patch.object(sync, "push_now", side_effect=ChatAPIError("not running")):
            sync.schedule_push("test")
            self._settle()   # the failure is logged, the caller never sees it


class SignalTests(TransactionTestCase):
    """on_commit means the push waits for the transaction, and skips a rollback.

    Importing chat.signals is what connects the receivers (that is what
    ChatConfig.ready does when chat is on), so the test does it explicitly rather
    than depending on the environment it runs in.
    """

    def setUp(self):
        import chat.signals  # noqa: F401  (connects the receivers)

        self.project = ProjectFactory()
        self.scheduled = []
        patcher = patch.object(sync, "schedule_push", lambda reason="": self.scheduled.append(reason))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saving_a_connection_schedules_a_push(self):
        conn = ModelConnectionFactory(project=self.project, base_url="https://a.example/v1")
        self.assertTrue(any(str(conn.pk) in reason for reason in self.scheduled))

    def test_deleting_a_connection_schedules_a_push(self):
        conn = ModelConnectionFactory(project=self.project, base_url="https://a.example/v1")
        self.scheduled.clear()
        conn.delete()
        self.assertEqual(len(self.scheduled), 1)

    def test_registering_a_model_schedules_a_push(self):
        conn = ModelConnectionFactory(project=self.project, base_url="https://a.example/v1")
        self.scheduled.clear()
        RegisteredModelFactory(connection=conn, project=self.project, model_id="gpt-4o")
        self.assertEqual(len(self.scheduled), 1)

    def test_a_rolled_back_change_pushes_nothing(self):
        from django.db import transaction

        try:
            with transaction.atomic():
                ModelConnectionFactory(project=self.project, base_url="https://a.example/v1")
                raise RuntimeError("rolled back")
        except RuntimeError:
            pass
        self.assertEqual(self.scheduled, [])


class PushNowTests(TestCase):
    def test_without_a_superuser_it_says_so(self):
        UserFactory(username="ordinary", is_superuser=False)
        with self.assertRaises(ChatAPIError) as caught:
            sync.push_now()
        self.assertIn("No superuser", str(caught.exception))
