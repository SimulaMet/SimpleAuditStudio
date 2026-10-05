"""Tests for the Super Admin page (server-rendered, /admin-settings/)."""
from unittest import mock

from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    RegisteredModelFactory,
    ScenarioFactory,
    UserFactory,
)
from infra.tests.utils import login, superuser


class AdminPageAccessTest(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get("/admin-settings/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)

    def test_non_superuser_forbidden(self):
        user = UserFactory()
        user.set_password("testpass123")
        user.save()
        login(self.client, user)
        resp = self.client.get("/admin-settings/")
        self.assertEqual(resp.status_code, 403)

    def test_superuser_sees_overview(self):
        admin = superuser()
        login(self.client, admin)
        resp = self.client.get("/admin-settings/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Admin Settings")
        self.assertContains(resp, "Per-workspace usage")

    def test_superuser_workspaces_tab(self):
        admin = superuser()
        project = ProjectFactory(name="Acme")
        MembershipFactory(user=admin, project=project, role="admin")
        login(self.client, admin)
        resp = self.client.get("/admin-settings/?tab=workspaces")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme")

    def test_superuser_users_tab_lists_users(self):
        admin = superuser()
        UserFactory(username="jane")
        login(self.client, admin)
        resp = self.client.get("/admin-settings/?tab=users")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "jane")
        self.assertContains(resp, "Super admin")

    def test_invalid_tab_falls_back_to_overview(self):
        admin = superuser()
        login(self.client, admin)
        resp = self.client.get("/admin-settings/?tab=bogus")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Per-workspace usage")

    @mock.patch("chat.api.ChatAPI.as_user")
    def test_web_search_tab_reads_and_updates_openwebui(self, as_user):
        admin = superuser()
        api = as_user.return_value
        api.retrieval_config.return_value = {
            "ENABLE_WEB_SEARCH": False,
            "WEB_SEARCH_ENGINE": "duckduckgo",
            "WEB_SEARCH_RESULT_COUNT": 5,
            "WEB_SEARCH_DOMAIN_FILTER_LIST": ["example.com"],
        }
        login(self.client, admin)

        resp = self.client.get("/admin-settings/?tab=web-search")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "duckduckgo")

        resp = self.client.post("/admin-settings/", {
            "tab": "web-search",
            "ENABLE_WEB_SEARCH": "on",
            "WEB_SEARCH_ENGINE": "brave",
            "WEB_SEARCH_RESULT_COUNT": "3",
            "WEB_SEARCH_DOMAIN_FILTER_LIST": "example.com, docs.example.com",
        })
        self.assertEqual(resp.status_code, 302)
        api.update_retrieval_config.assert_called_once_with({
            "ENABLE_WEB_SEARCH": True,
            "WEB_SEARCH_ENGINE": "brave",
            "WEB_SEARCH_RESULT_COUNT": 3,
            "WEB_SEARCH_DOMAIN_FILTER_LIST": ["example.com", "docs.example.com"],
        })

    @mock.patch("chat.api.ChatAPI.as_user")
    def test_subagent_tab_reads_and_updates_openwebui(self, as_user):
        admin = superuser()
        api = as_user.return_value
        api.subagents_config.return_value = {
            "ENABLE_SUBAGENTS": False,
            "SUBAGENTS_BACKGROUND_ENABLED": False,
            "SUBAGENTS_MAX_CONCURRENT": 20,
            "SUBAGENTS_MAX_ASYNC": 20,
            "SUBAGENTS_MAX_ITERATIONS": 30,
            "SUBAGENTS_MAX_OUTPUT": 30000,
            "SUBAGENTS_SYSTEM_PROMPT": "",
        }
        login(self.client, admin)

        resp = self.client.get("/admin-settings/?tab=sub-agents")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Execution limits")

        resp = self.client.post("/admin-settings/", {
            "tab": "sub-agents",
            "ENABLE_SUBAGENTS": "on",
            "SUBAGENTS_MAX_CONCURRENT": "4",
            "SUBAGENTS_MAX_ASYNC": "2",
            "SUBAGENTS_MAX_ITERATIONS": "10",
            "SUBAGENTS_MAX_OUTPUT": "10000",
            "SUBAGENTS_SYSTEM_PROMPT": "Stay concise.",
        })
        self.assertEqual(resp.status_code, 302)
        api.update_subagents_config.assert_called_once_with({
            "ENABLE_SUBAGENTS": True,
            "SUBAGENTS_BACKGROUND_ENABLED": False,
            "SUBAGENTS_MAX_CONCURRENT": 4,
            "SUBAGENTS_MAX_ASYNC": 2,
            "SUBAGENTS_MAX_ITERATIONS": 10,
            "SUBAGENTS_MAX_OUTPUT": 10000,
            "SUBAGENTS_SYSTEM_PROMPT": "Stay concise.",
        })


class AdminPageStatsTest(TestCase):
    def setUp(self):
        self.admin = superuser()
        self.client = Client(SERVER_NAME="localhost")
        login(self.client, self.admin)

    def test_counts_reflect_data(self):
        project = ProjectFactory(name="Acme")
        MembershipFactory(user=self.admin, project=project, role="admin")
        ScenarioFactory(project=project)
        model = RegisteredModelFactory(project=project)
        run = AuditRunFactory(
            project=project,
            target_model=model,
            auditor_model=model,
            judge_model=model,
        )
        run.status = "completed"
        run.save()

        resp = self.client.get("/admin-settings/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme")
