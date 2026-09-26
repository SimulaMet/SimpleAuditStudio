"""Tests for workspace archive read-only enforcement + delete-empty behavior."""
import json

from django.test import Client, TestCase

from accounts.models import Project
from infra.tests.factories import (
    MembershipFactory,
    ProjectFactory,
    ScenarioFactory,
    UserFactory,
)


def _login(client, user, pw="testpass123"):
    creds = {"username": user.username, "password": pw}
    client.login(**creds)


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def _scenario_payload():
    return {
        "title": "New scenario",
        "description": "desc",
        "expected_behavior": ["be helpful"],
    }


class ArchiveReadOnlyTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory(name="Acme")
        self.member = UserFactory(username="member")
        self.member.set_password("testpass123")
        self.member.save()
        MembershipFactory(user=self.member, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")

    def test_member_can_create_scenario_when_active(self):
        _login(self.client, self.member)
        resp = _post(self.client, f"/api/projects/{self.project.id}/scenarios/create/", _scenario_payload())
        self.assertEqual(resp.status_code, 201)

    def test_member_cannot_create_scenario_when_archived(self):
        self.project.archived = True
        self.project.save()
        _login(self.client, self.member)
        resp = _post(self.client, f"/api/projects/{self.project.id}/scenarios/create/", _scenario_payload())
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "workspace_archived")

    def test_member_can_still_read_when_archived(self):
        ScenarioFactory(project=self.project)
        self.project.archived = True
        self.project.save()
        _login(self.client, self.member)
        resp = self.client.get(f"/api/projects/{self.project.id}/scenarios/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_superuser_can_create_scenario_when_archived(self):
        admin = UserFactory(username="boss")
        admin.is_superuser = True
        admin.is_staff = True
        admin.set_password("testpass123")
        admin.save()
        MembershipFactory(user=admin, project=self.project, role="admin")
        self.project.archived = True
        self.project.save()
        _login(self.client, admin)
        resp = _post(self.client, f"/api/projects/{self.project.id}/scenarios/create/", _scenario_payload())
        self.assertEqual(resp.status_code, 201)


class DeleteWorkspaceTest(TestCase):
    def setUp(self):
        self.admin = UserFactory(username="boss")
        self.admin.is_superuser = True
        self.admin.is_staff = True
        self.admin.set_password("testpass123")
        self.admin.save()
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.admin)

    def test_delete_empty_workspace(self):
        project = ProjectFactory(name="Empty")
        MembershipFactory(user=self.admin, project=project, role="admin")
        resp = self.client.delete(f"/api/projects/{project.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Project.objects.filter(pk=project.id).exists())

    def test_delete_non_empty_workspace_refused(self):
        project = ProjectFactory(name="Full")
        MembershipFactory(user=self.admin, project=project, role="admin")
        ScenarioFactory(project=project)
        resp = self.client.delete(f"/api/projects/{project.id}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "workspace_not_empty")
        self.assertTrue(Project.objects.filter(pk=project.id).exists())


class ArchivedBadgeTest(TestCase):
    def setUp(self):
        self.user = UserFactory(username="member")
        self.user.set_password("testpass123")
        self.user.save()
        self.project = ProjectFactory(name="Acme")
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.user)

    def test_workspace_list_includes_archived_flag(self):
        self.project.archived = True
        self.project.save()
        resp = self.client.get("/api/projects/")
        self.assertEqual(resp.status_code, 200)
        items = {w["slug"]: w for w in resp.json()}
        self.assertTrue(items[self.project.slug]["archived"])
