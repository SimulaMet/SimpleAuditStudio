"""Tests for the Super Admin page (server-rendered, /admin-settings/)."""
from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    RegisteredModelFactory,
    ScenarioFactory,
    UserFactory,
)


def _login(client, user, pw="testpass123"):
    creds = {"username": user.username, "password": pw}
    client.login(**creds)


def _superuser():
    user = UserFactory()
    user.is_superuser = True
    user.is_staff = True
    user.set_password("testpass123")
    user.save()
    return user


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
        _login(self.client, user)
        resp = self.client.get("/admin-settings/")
        self.assertEqual(resp.status_code, 403)

    def test_superuser_sees_overview(self):
        admin = _superuser()
        _login(self.client, admin)
        resp = self.client.get("/admin-settings/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Admin Settings")
        self.assertContains(resp, "Per-workspace usage")

    def test_superuser_workspaces_tab(self):
        admin = _superuser()
        project = ProjectFactory(name="Acme")
        MembershipFactory(user=admin, project=project, role="admin")
        _login(self.client, admin)
        resp = self.client.get("/admin-settings/?tab=workspaces")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme")

    def test_superuser_users_tab_lists_users(self):
        admin = _superuser()
        UserFactory(username="jane")
        _login(self.client, admin)
        resp = self.client.get("/admin-settings/?tab=users")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "jane")
        self.assertContains(resp, "Super admin")

    def test_invalid_tab_falls_back_to_overview(self):
        admin = _superuser()
        _login(self.client, admin)
        resp = self.client.get("/admin-settings/?tab=bogus")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Per-workspace usage")


class AdminPageStatsTest(TestCase):
    def setUp(self):
        self.admin = _superuser()
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.admin)

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
