"""Agent page: Open WebUI sync throttle."""
from unittest import mock

from django.test import TestCase, override_settings

from infra import ui
from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory


class ThrottledSyncTests(TestCase):
    def setUp(self):
        ui._openwebui_sync_cache.clear()
        self.addCleanup(ui._openwebui_sync_cache.clear)
        self.project = ProjectFactory()
        self.user = UserFactory()
        MembershipFactory(user=self.user, project=self.project)

    @override_settings(OPENWEBUI_SYNC_TTL_S=60)
    def test_second_load_reuses_summary(self):
        summary = {"lost": [], "visible": {"knowledge_base": {"k"}, "tool": set()}}
        with mock.patch.object(ui, "_sync_openwebui_resources", return_value=summary) as sync:
            self.assertEqual(ui._throttled_openwebui_sync(self.project, self.user), (summary, True))
            self.assertEqual(ui._throttled_openwebui_sync(self.project, self.user), (summary, False))
        self.assertEqual(sync.call_count, 1)

    @override_settings(OPENWEBUI_SYNC_TTL_S=0)
    def test_zero_ttl_always_syncs(self):
        with mock.patch.object(ui, "_sync_openwebui_resources", return_value={}) as sync:
            ui._throttled_openwebui_sync(self.project, self.user)
            ui._throttled_openwebui_sync(self.project, self.user)
        self.assertEqual(sync.call_count, 2)
