"""Tests for deleting model connections from the models page.

Deleting a connection cascades to its RegisteredModels, but AuditRun pins
models via RESTRICT FKs (immutable experiment records). The view must catch
the resulting ProtectedError and show a friendly banner instead of 500ing.
"""
from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)
from model_registry.models import ModelConnection


class ConnectionDeleteTest(TestCase):
    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=pw)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()

    def test_delete_unreferenced_connection_succeeds(self):
        conn = ModelConnectionFactory(project=self.project, name="unused-conn")
        RegisteredModelFactory(connection=conn, project=self.project)

        resp = self.client.post(f"/connections/{conn.id}/delete/")

        assert resp.status_code == 302
        assert resp.url == "/connections/"
        assert not conn.__class__.objects.filter(pk=conn.id).exists()

    def test_delete_referenced_connection_is_blocked_with_message(self):
        conn = ModelConnectionFactory(project=self.project, name="pinned-conn")
        pinned_model = RegisteredModelFactory(connection=conn, project=self.project)
        AuditRunFactory(project=self.project, target_model=pinned_model)

        resp = self.client.post(f"/connections/{conn.id}/delete/")

        assert resp.status_code == 302
        assert resp.url == "/connections/"
        # Connection and its models must still exist.
        assert conn.__class__.objects.filter(pk=conn.id).exists()
        assert pinned_model.__class__.objects.filter(pk=pinned_model.id).exists()
        # The error banner is rendered on the next page load (base.html iterates
        # the messages framework), so follow the redirect and check the body.
        get_resp = self.client.get("/connections/")
        body = get_resp.content.decode()
        assert "runs or monitors use it" in body

    def test_delete_other_project_connection_is_noop(self):
        other_project = ProjectFactory()
        conn = ModelConnectionFactory(project=other_project, name="foreign-conn")

        resp = self.client.post(f"/connections/{conn.id}/delete/")

        assert resp.status_code == 302
        assert conn.__class__.objects.filter(pk=conn.id).exists()


class RegisteredModelDeleteTest(TestCase):
    """Deleting a single model (not its connection) must also be guarded."""

    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=pw)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()

    def test_delete_unreferenced_model_succeeds(self):
        from model_registry.models import RegisteredModel

        conn = ModelConnectionFactory(project=self.project, name="conn-a")
        rm = RegisteredModelFactory(connection=conn, project=self.project)

        resp = self.client.post("/connections/", {"action": "delete_model", "rm_id": rm.id}, follow=True)

        assert resp.status_code == 200
        assert not RegisteredModel.objects.filter(pk=rm.id).exists()

    def test_delete_referenced_model_is_blocked_with_error(self):
        from model_registry.models import RegisteredModel

        conn = ModelConnectionFactory(project=self.project, name="conn-b")
        rm = RegisteredModelFactory(connection=conn, project=self.project)
        AuditRunFactory(project=self.project, target_model=rm)

        resp = self.client.post("/connections/", {"action": "delete_model", "rm_id": rm.id}, follow=True)

        assert resp.status_code == 200
        # Model must still exist.
        assert RegisteredModel.objects.filter(pk=rm.id).exists()
        body = resp.content.decode()
        assert "runs or monitors use it" in body


class DiscoverModelsKeyTests(TestCase):
    """The Connections page must never render API keys; discover resolves them server-side."""

    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="viewer")
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")
        self.conn = ModelConnection.objects.create(
            project=self.project, name="Secret conn", provider="openai",
            base_url="https://api.example.invalid/v1", api_key_direct="sk-super-secret-123",
        )

    def test_models_page_does_not_contain_the_key(self):
        page = self.client.get("/connections/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "sk-super-secret-123")
        self.assertContains(page, f'data-discover="{self.conn.id}"')

    def test_discover_uses_stored_key_for_own_connection_only(self):
        from unittest import mock

        fake = mock.MagicMock()
        fake.json.return_value = {"data": [{"id": "gpt-x"}]}
        with mock.patch("model_registry.services.httpx.get", return_value=fake) as get:
            resp = self.client.post("/connections/discover/", {"connection_id": self.conn.id})
        self.assertEqual(resp.json()["models"], ["gpt-x"])
        url, kwargs = get.call_args.args[0], get.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-super-secret-123")
        self.assertEqual(url, "https://api.example.invalid/v1/models")

        other = ModelConnection.objects.create(project=ProjectFactory(), name="Other", base_url="https://x.invalid/v1")
        self.assertEqual(self.client.post("/connections/discover/", {"connection_id": other.id}).status_code, 404)
