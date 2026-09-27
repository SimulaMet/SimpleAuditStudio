"""Every UI form that changes workspace data needs the admin or auditor role
(the same rule as the API), and model servers are only reachable through a
workspace the user belongs to."""
from unittest import mock

from django.test import Client, TestCase

from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    UserFactory,
)
from model_registry.models import ModelConnection
from scenarios.models import ScenarioSet


class _Base(TestCase):
    role = "viewer"

    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role=self.role)
        self.client = Client()
        self.client.force_login(self.user)


class ViewerWriteTests(_Base):
    def test_viewer_cannot_create_scenario_set(self):
        resp = self.client.post("/scenarios/set-create/", {"name": "Sneaky"}, follow=True)
        self.assertContains(resp, "Admin or auditor role required")
        self.assertFalse(ScenarioSet.objects.filter(name="Sneaky").exists())

    def test_viewer_cannot_add_connection(self):
        resp = self.client.post("/models/", {
            "action": "add_connection", "conn_name": "X", "conn_base_url": "https://api.example.com/v1",
        }, follow=True)
        self.assertContains(resp, "Admin or auditor role required")
        self.assertFalse(ModelConnection.objects.filter(project=self.project).exists())


class AuditorWriteTests(_Base):
    role = "auditor"

    def test_auditor_can_create_scenario_set(self):
        self.client.post("/scenarios/set-create/", {"name": "Allowed"})
        self.assertTrue(ScenarioSet.objects.filter(project=self.project, name="Allowed").exists())

    def test_connection_base_url_is_validated(self):
        resp = self.client.post("/models/", {"action": "add_connection", "conn_name": "Bad", "conn_base_url": "not a url"})
        self.assertContains(resp, "Base URL must be an http(s) URL")
        self.assertFalse(ModelConnection.objects.filter(project=self.project).exists())
        self.client.post("/models/", {"action": "add_connection", "conn_name": "Ok", "conn_base_url": "http://mock-model:8080/v1"})
        self.assertTrue(ModelConnection.objects.filter(project=self.project, name="Ok").exists())


class PingConnectionScopeTests(_Base):
    def test_cannot_ping_another_workspace_connection(self):
        other = ModelConnectionFactory()   # different workspace
        with mock.patch("model_registry.services.httpx.get") as get:
            resp = self.client.get(f"/api/models/ping-connection/{other.id}/")
        self.assertEqual(resp.status_code, 404)
        get.assert_not_called()

    def test_can_ping_own_connection(self):
        conn = ModelConnectionFactory(project=self.project)
        fake = mock.MagicMock()
        fake.json.return_value = {"data": [{"id": "m1"}]}
        with mock.patch("model_registry.services.httpx.get", return_value=fake):
            resp = self.client.get(f"/api/models/ping-connection/{conn.id}/")
        self.assertEqual(resp.json()["status"], "up")
