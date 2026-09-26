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


class ConnectionDeleteTest(TestCase):
    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="owner")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=pw)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()

    def test_delete_unreferenced_connection_succeeds(self):
        conn = ModelConnectionFactory(project=self.project, name="unused-conn")
        RegisteredModelFactory(connection=conn, project=self.project)

        resp = self.client.post(f"/models/connection-delete/{conn.id}/")

        assert resp.status_code == 302
        assert resp.url == "/models/"
        assert not conn.__class__.objects.filter(pk=conn.id).exists()

    def test_delete_referenced_connection_is_blocked_with_message(self):
        conn = ModelConnectionFactory(project=self.project, name="pinned-conn")
        pinned_model = RegisteredModelFactory(connection=conn, project=self.project)
        AuditRunFactory(project=self.project, target_model=pinned_model)

        resp = self.client.post(f"/models/connection-delete/{conn.id}/")

        assert resp.status_code == 302
        assert resp.url == "/models/"
        # Connection and its models must still exist.
        assert conn.__class__.objects.filter(pk=conn.id).exists()
        assert pinned_model.__class__.objects.filter(pk=pinned_model.id).exists()
        # The error banner is rendered on the next page load (base.html iterates
        # the messages framework), so follow the redirect and check the body.
        get_resp = self.client.get("/models/")
        body = get_resp.content.decode()
        assert "referenced by audit runs" in body

    def test_delete_other_project_connection_is_noop(self):
        other_project = ProjectFactory()
        conn = ModelConnectionFactory(project=other_project, name="foreign-conn")

        resp = self.client.post(f"/models/connection-delete/{conn.id}/")

        assert resp.status_code == 302
        assert conn.__class__.objects.filter(pk=conn.id).exists()
