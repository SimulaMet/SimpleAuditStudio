"""Tests for the Super Admin REST API (stats, user CRUD, archive)."""
import json

from django.test import Client, TestCase

from accounts.models import User
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


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _patch(client, url, payload):
    return client.patch(url, data=json.dumps(payload), content_type="application/json")


class AdminStatsApiTest(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")

    def test_requires_superuser(self):
        user = UserFactory()
        user.set_password("testpass123")
        user.save()
        _login(self.client, user)
        resp = self.client.get("/api/admin/stats/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "super_admin_required")

    def test_counts_only_no_content(self):
        admin = _superuser()
        project = ProjectFactory(name="Acme")
        MembershipFactory(user=admin, project=project, role="admin")
        ScenarioFactory(project=project)
        model = RegisteredModelFactory(project=project)
        run = AuditRunFactory(project=project, target_model=model, auditor_model=model, judge_model=model)
        run.status = "completed"
        run.save()
        _login(self.client, admin)
        resp = self.client.get("/api/admin/stats/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["platform"]["scenario_count"], 1)
        self.assertEqual(data["platform"]["audit_run_total"], 1)
        ws = next(w for w in data["workspaces"] if w["name"] == "Acme")
        self.assertEqual(ws["name"], "Acme")
        self.assertEqual(ws["scenario_count"], 1)
        self.assertEqual(ws["audit_run_count"], 1)
        self.assertEqual(ws["audit_completed"], 1)
        # Counts only: no scenario/model content fields leak.
        self.assertNotIn("scenarios", data)
        self.assertNotIn("models", data)


class AdminUserCrudApiTest(TestCase):
    def setUp(self):
        self.admin = _superuser()
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.admin)

    def test_create_user(self):
        resp = _post(self.client, "/api/admin/users/", {"username": "newbie", "email": "newbie@test.com", "password": "Str0ng-pass-123"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["username"], "newbie")

    def test_create_user_username_taken(self):
        UserFactory(username="taken")
        resp = _post(self.client, "/api/admin/users/", {"username": "taken", "password": "Str0ng-pass-123"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "username_taken")

    def test_create_user_email_taken(self):
        UserFactory(username="other", email="dup@test.com")
        resp = _post(self.client, "/api/admin/users/", {"username": "newbie", "email": "dup@test.com", "password": "Str0ng-pass-123"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "email_taken")

    def test_update_user_email(self):
        target = UserFactory(username="jane", email="jane@old.com")
        resp = _patch(self.client, f"/api/admin/users/{target.id}/", {"email": "jane@new.com"})
        self.assertEqual(resp.status_code, 200)
        target.refresh_from_db()
        self.assertEqual(target.email, "jane@new.com")

    def test_deactivate_user(self):
        target = UserFactory(username="jane")
        resp = _patch(self.client, f"/api/admin/users/{target.id}/", {"is_active": False})
        self.assertEqual(resp.status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_demote_self_forbidden(self):
        resp = _patch(self.client, f"/api/admin/users/{self.admin.id}/", {"is_superuser": False})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "cannot_demote_self")

    def test_demote_superuser_when_another_remains_allowed(self):
        # Two superusers exist; demoting one is allowed (a super admin
        # remains). The last-superuser guard only blocks when the count
        # would drop to zero, which for a non-self target is unreachable
        # (the only remaining superuser would be self, covered by
        # cannot_demote_self).
        other = UserFactory(username="boss2")
        other.is_superuser = True
        other.save()
        resp = _patch(self.client, f"/api/admin/users/{other.id}/", {"is_superuser": False})
        self.assertEqual(resp.status_code, 200)
        other.refresh_from_db()
        self.assertFalse(other.is_superuser)

    def test_delete_user(self):
        target = UserFactory(username="jane")
        resp = self.client.delete(f"/api/admin/users/{target.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(User.objects.filter(pk=target.id).exists())

    def test_delete_self_forbidden(self):
        resp = self.client.delete(f"/api/admin/users/{self.admin.id}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "cannot_delete_self")


class AdminArchiveApiTest(TestCase):
    def setUp(self):
        self.admin = _superuser()
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.admin)

    def test_archive_and_unarchive(self):
        project = ProjectFactory(name="Acme")
        MembershipFactory(user=self.admin, project=project, role="admin")
        resp = self.client.post(f"/api/admin/workspaces/{project.id}/archive/")
        self.assertEqual(resp.status_code, 200)
        project.refresh_from_db()
        self.assertTrue(project.archived)

        resp = self.client.post(f"/api/admin/workspaces/{project.id}/unarchive/")
        self.assertEqual(resp.status_code, 200)
        project.refresh_from_db()
        self.assertFalse(project.archived)

    def test_archive_requires_superuser(self):
        user = UserFactory()
        user.set_password("testpass123")
        user.save()
        project = ProjectFactory(name="Acme")
        MembershipFactory(user=user, project=project, role="admin")
        _login(self.client, user)
        resp = self.client.post(f"/api/admin/workspaces/{project.id}/archive/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "super_admin_required")
