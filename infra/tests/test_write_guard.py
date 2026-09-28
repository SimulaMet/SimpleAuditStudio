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
        resp = self.client.post("/models/", {"action": "add_connection", "conn_name": "Bad", "conn_base_url": "not a url"}, follow=True)
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


class ModelsPageTests(_Base):
    role = "admin"

    def test_bulk_add_skips_existing_and_redirects(self):
        from model_registry.models import RegisteredModel

        conn = ModelConnectionFactory(project=self.project)
        RegisteredModel.objects.create(connection=conn, project=self.project, model_id="a", display_name="a")
        resp = self.client.post("/models/", {"action": "add_models", "conn_id": conn.id, "model_id": ["a", "b", "c"]})
        self.assertEqual(resp.status_code, 302)   # redirect after save: refresh won't resubmit
        self.assertEqual(sorted(conn.models.values_list("model_id", flat=True)), ["a", "b", "c"])
        page = self.client.get(resp.url)
        self.assertContains(page, "Added 2 models")

    def test_key_mode_switches_clear_the_other_source(self):
        conn = ModelConnectionFactory(project=self.project, api_key_direct="sk-old", secret_reference="")
        data = {"action": "edit_connection", "conn_id": conn.id, "conn_name": conn.name,
                "conn_base_url": "https://api.example.com/v1", "conn_enabled": "1"}
        self.client.post("/models/", {**data, "key_mode": "stored", "conn_api_key": ""})
        conn.refresh_from_db()
        self.assertEqual(conn.api_key_direct, "sk-old")   # blank keeps the stored key
        resp = self.client.post("/models/", {**data, "key_mode": "env", "conn_secret_ref": ""}, follow=True)
        self.assertContains(resp, "Enter the environment variable")
        conn.refresh_from_db()
        self.assertEqual(conn.api_key_direct, "sk-old")   # rejected: nothing changed
        self.client.post("/models/", {**data, "key_mode": "env", "conn_secret_ref": "MY_KEY"})
        conn.refresh_from_db()
        self.assertEqual((conn.api_key_direct, conn.secret_reference), ("", "MY_KEY"))
        self.client.post("/models/", {**data, "key_mode": "none"})
        conn.refresh_from_db()
        self.assertEqual((conn.api_key_direct, conn.secret_reference), ("", ""))

    def test_descriptions_saved_and_shown(self):
        from infra.tests.factories import RegisteredModelFactory

        conn = ModelConnectionFactory(project=self.project)
        self.client.post("/models/", {"action": "edit_connection", "conn_id": conn.id, "conn_name": conn.name,
                                      "conn_base_url": "https://api.example.com/v1", "conn_enabled": "1",
                                      "key_mode": "none", "conn_description": "  Team account  "})
        rm = RegisteredModelFactory(connection=conn, project=self.project)
        self.client.post("/models/", {"action": "edit_model", "rm_id": rm.id, "model_id_new": rm.model_id,
                                      "model_display_name": "Fast", "model_description": "Cheap & quick"})
        self.client.post("/models/", {"action": "add_models", "conn_id": conn.id, "model_id": ["solo"],
                                      "model_description": "Single add"})
        conn.refresh_from_db()
        rm.refresh_from_db()
        self.assertEqual(conn.description, "Team account")
        self.assertEqual((rm.display_name, rm.description), ("Fast", "Cheap & quick"))
        self.assertEqual(conn.models.get(model_id="solo").description, "Single add")
        page = self.client.get("/models/")
        self.assertContains(page, "Team account")
        self.assertContains(page, "Cheap &amp; quick")
        # Bulk adds don't copy one description onto every model.
        self.client.post("/models/", {"action": "add_models", "conn_id": conn.id, "model_id": ["x", "y"],
                                      "model_description": "ignored"})
        self.assertFalse(conn.models.filter(model_id__in=["x", "y"]).exclude(description="").exists())

    def test_used_model_shows_lock_not_delete(self):
        from infra.tests.factories import AuditRunFactory, RegisteredModelFactory

        conn = ModelConnectionFactory(project=self.project)
        used = RegisteredModelFactory(connection=conn, project=self.project)
        AuditRunFactory(project=self.project, target_model=used)
        page = self.client.get("/models/").content.decode()
        self.assertIn("1 run", page)
        self.assertNotIn(f'value="{used.id}">\n                <button title="Remove model"', page)
        self.assertIn("kept for reproducibility", page)
