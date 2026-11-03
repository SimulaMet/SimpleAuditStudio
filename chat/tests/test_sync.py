"""Connections change in Studio, chat follows — without blocking the save.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test chat.tests.test_sync
"""
import pathlib
import time
from unittest import mock
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from chat import sync
from chat.api import ChatAPIError
from chat.sync import NoAdminError
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


class RunTests(TestCase):
    """_run is the background worker: it must never raise, and it must tell
    a misconfiguration (no superuser) apart from a transient skip."""

    def test_no_superuser_is_logged_loudly_with_an_actionable_message(self):
        with patch.object(
            sync, "push_now",
            side_effect=NoAdminError("No superuser to act as; Open WebUI's provider config needs an admin."),
        ), self.assertLogs("chat.sync", level="WARNING") as captured:
            sync._run("test")
        self.assertTrue(
            any("no Studio superuser" in line for line in captured.output),
            captured.output,
        )
        self.assertTrue(
            any("manage.py sync_chat_models" in line for line in captured.output),
            captured.output,
        )

    def test_a_transient_failure_is_a_quiet_skip_not_a_no_superuser_warning(self):
        with patch.object(sync, "push_now", side_effect=ChatAPIError("not running")), \
                self.assertLogs("chat.sync", level="INFO") as captured:
            sync._run("test")
        self.assertTrue(
            any("Chat model sync skipped" in line and "not running" in line for line in captured.output),
            captured.output,
        )
        self.assertFalse(
            any("no Studio superuser" in line for line in captured.output),
            captured.output,
        )


class ReconcileModelIdsTests(TestCase):
    """push_now warns when a pinned Studio model id is not one Open WebUI
    registers — the case where the ?models= pin would silently no-op."""

    def _push(self, registered, model_ids):
        api = mock.Mock()
        api.push_connections.return_value = {"pushed": 1, "kept": 0}
        api.list_models.return_value = registered
        with patch.object(sync, "ChatAPI") as chat_api, \
                patch.object(sync, "_admin", return_value=object()):
            chat_api.as_user.return_value = api
            sync.push_now([
                {"id": 1, "name": "OpenAI", "base_url": "https://api.openai.com/v1",
                 "api_key": "sk-x", "enabled": True, "model_ids": model_ids},
            ])
        return api

    def test_a_missing_model_id_is_logged_loudly(self):
        with self.assertLogs("chat.sync", level="WARNING") as captured:
            api = self._push(["gpt-4o"], ["gpt-4o", "studio-internal-id"])
        self.assertTrue(
            any("will not work for connection 'OpenAI'" in line
                and "studio-internal-id" in line
                for line in captured.output),
            captured.output,
        )
        api.list_models.assert_called_once()

    def test_all_ids_registered_logs_no_warning(self):
        import logging

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Capture(level=logging.WARNING)
        with mock.patch.object(logging.getLogger("chat.sync"), "handlers", [handler]):
            self._push(["gpt-4o", "gpt-4o-mini"], ["gpt-4o", "gpt-4o-mini"])
        self.assertFalse(
            any("will not work" in line for line in records),
            records,
        )

    def test_a_connection_without_model_ids_is_not_checked(self):
        api = self._push(["gpt-4o"], [])
        api.list_models.assert_not_called()

    def test_a_models_endpoint_failure_skips_reconciliation(self):
        api = mock.Mock()
        api.push_connections.return_value = {"pushed": 1, "kept": 0}
        api.list_models.side_effect = ChatAPIError("not ready")
        with patch.object(sync, "ChatAPI") as chat_api, \
                patch.object(sync, "_admin", return_value=object()):
            chat_api.as_user.return_value = api
            sync.push_now([
                {"id": 1, "name": "OpenAI", "base_url": "https://api.openai.com/v1",
                 "api_key": "sk-x", "enabled": True, "model_ids": ["gpt-4o"]},
            ])
        # The push itself still reports success.
        self.assertEqual(api.push_connections.call_count, 1)


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


class RagOverridesFunctionTests(TestCase):
    def _api(self, existing: dict | None):
        api = mock.MagicMock()
        api.list_functions.return_value = [existing] if existing else []
        api.get_function.return_value = existing or {}
        return api

    def test_missing_function_is_installed_then_activated_and_made_global(self):
        api = self._api(None)
        api.create_function.return_value = {"id": sync.RAG_OVERRIDES_FUNCTION_ID}
        report = sync.ensure_rag_overrides_function(api)
        api.create_function.assert_called_once()
        self.assertEqual(api.create_function.call_args.kwargs["function_id"], sync.RAG_OVERRIDES_FUNCTION_ID)
        api.request.assert_any_call("POST", f"/api/v1/functions/id/{sync.RAG_OVERRIDES_FUNCTION_ID}/toggle")
        api.request.assert_any_call("POST", f"/api/v1/functions/id/{sync.RAG_OVERRIDES_FUNCTION_ID}/toggle/global")
        self.assertIn("installed", report)

    def test_current_active_global_function_is_left_alone(self):
        existing = {
            "id": sync.RAG_OVERRIDES_FUNCTION_ID,
            "content": pathlib.Path(sync.RAG_OVERRIDES_FUNCTION_FILE).read_text(encoding="utf-8"),
            "is_active": True,
            "is_global": True,
        }
        api = self._api(existing)
        report = sync.ensure_rag_overrides_function(api)
        api.create_function.assert_not_called()
        api.update_function_content.assert_not_called()
        api.request.assert_not_called()
        self.assertIn("up to date", report)

    def test_a_chat_that_is_down_does_not_raise(self):
        api = mock.MagicMock()
        api.list_functions.side_effect = ChatAPIError("chat is down")
        report = sync.ensure_rag_overrides_function(api)
        self.assertIn("not synced", report)


class StartupSyncTests(TestCase):
    def test_chat_off_does_nothing(self):
        with patch.object(sync.config, "ENABLED", False), patch.object(sync, "push_now") as push:
            self.assertEqual(sync.startup_sync(), [])
        push.assert_not_called()

    def test_chat_down_is_reported_not_raised(self):
        with patch.object(sync.config, "ENABLED", True), patch.object(
            sync, "push_now", side_effect=ChatAPIError("chat is down")
        ):
            self.assertEqual(sync.startup_sync(), ["Chat not synced at startup: chat is down"])


class AgentOtlpCredentialTests(TestCase):
    def setUp(self):
        from infra.tests.factories import ProjectFactory, UserFactory
        from model_registry.models import ModelConnection

        self.admin = UserFactory(username="chat-admin", is_superuser=True)
        self.project = ProjectFactory()
        self.connection = ModelConnection.objects.create(
            project=self.project, name="Open WebUI Agents", provider="openai",
            base_url="http://open-webui/chat/api", enabled=True,
        )

    def test_chat_tracing_off_creates_nothing(self):
        from model_registry.models import OTLPCredential

        with patch.object(sync.config, "OTLP_ENABLED", False):
            self.assertEqual(sync.ensure_agent_otlp_credentials(), 0)
        self.assertFalse(OTLPCredential.objects.exists())

    def test_creates_one_open_credential_and_is_idempotent(self):
        from model_registry.models import OTLPCredential

        with patch.object(sync.config, "OTLP_ENABLED", True), patch.object(sync.config, "ENABLED", True):
            self.assertEqual(sync.ensure_agent_otlp_credentials(), 1)
            self.assertEqual(sync.ensure_agent_otlp_credentials(), 0)
        cred = OTLPCredential.objects.get(connection=self.connection)
        self.assertEqual(cred.auth_mode, OTLPCredential.AuthMode.NONE)
        self.assertTrue(cred.enabled)
