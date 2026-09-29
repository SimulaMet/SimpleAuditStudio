"""Tests for model-connection sharing across workspaces.

A connection's ``visibility`` controls which workspaces can see and use its
models:
  * workspace (default) — only the owning workspace,
  * admins              — any workspace whose user is an admin,
  * public              — every workspace.
Plus an explicit ``shared_with`` list of specific workspaces.

Consumers see the description but cannot edit a shared connection; only the
owner workspace's admins may change it. The owner's API key is used by all
consumers.
"""
from unittest import mock

from django.test import Client, TestCase

from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)
from model_registry.models import ModelConnection
from model_registry.services import (
    admin_workspaces,
    can_edit_connection,
    visible_connection_ids_for,
    visible_connections_for,
)


def _login(client, user):
    user.set_password("testpass123")
    user.save()
    client.login(username=user.username, password="testpass123")


class VisibilityHelperTests(TestCase):
    """Unit tests for the visibility query helpers."""

    def setUp(self):
        self.owner_ws = ProjectFactory(name="Owner WS")
        self.other_ws = ProjectFactory(name="Other WS")
        self.third_ws = ProjectFactory(name="Third WS")
        self.owner_user = UserFactory()
        self.other_user = UserFactory()
        # owner_user is admin of owner_ws and other_ws; other_user is admin of third_ws.
        MembershipFactory(user=self.owner_user, project=self.owner_ws, role="admin")
        MembershipFactory(user=self.owner_user, project=self.other_ws, role="admin")
        MembershipFactory(user=self.other_user, project=self.third_ws, role="admin")
        self.conn = ModelConnectionFactory(project=self.owner_ws, name="Shared conn")

    def test_workspace_visibility_only_owner_sees_it(self):
        self.conn.visibility = ModelConnection.Visibility.WORKSPACE
        self.conn.save()
        seen = {c.id for c in visible_connections_for(self.owner_user, self.owner_ws)}
        self.assertIn(self.conn.id, seen)
        seen_other = {c.id for c in visible_connections_for(self.other_user, self.other_ws)}
        self.assertNotIn(self.conn.id, seen_other)

    def test_public_visibility_everyone_sees_it(self):
        self.conn.visibility = ModelConnection.Visibility.PUBLIC
        self.conn.save()
        for user, ws in ((self.owner_user, self.owner_ws), (self.other_user, self.other_ws), (self.other_user, self.third_ws)):
            seen = {c.id for c in visible_connections_for(user, ws)}
            self.assertIn(self.conn.id, seen, f"{user.username} in {ws.name} should see public conn")

    def test_admins_visibility_admins_of_any_ws_see_it(self):
        self.conn.visibility = ModelConnection.Visibility.ADMINS
        self.conn.save()
        # owner_user is an admin (of owner_ws / other_ws) -> sees it in other_ws.
        seen = {c.id for c in visible_connections_for(self.owner_user, self.other_ws)}
        self.assertIn(self.conn.id, seen)
        # A plain viewer of other_ws does not.
        viewer = UserFactory()
        MembershipFactory(user=viewer, project=self.other_ws, role="viewer")
        seen_viewer = {c.id for c in visible_connections_for(viewer, self.other_ws)}
        self.assertNotIn(self.conn.id, seen_viewer)

    def test_explicit_shared_with(self):
        self.conn.visibility = ModelConnection.Visibility.WORKSPACE
        self.conn.shared_with.add(self.other_ws)
        self.conn.save()
        seen = {c.id for c in visible_connections_for(self.other_user, self.other_ws)}
        self.assertIn(self.conn.id, seen)
        seen_third = {c.id for c in visible_connections_for(self.other_user, self.third_ws)}
        self.assertNotIn(self.conn.id, seen_third)

    def test_visible_connection_ids_includes_owner_and_shared(self):
        self.conn.visibility = ModelConnection.Visibility.PUBLIC
        self.conn.save()
        ids = set(visible_connection_ids_for(self.other_ws))
        self.assertIn(self.conn.id, ids)
        # Owner's own connections are always included.
        own = ModelConnectionFactory(project=self.other_ws, name="Own conn")
        self.assertIn(own.id, set(visible_connection_ids_for(self.other_ws)))

    def test_can_edit_connection_owner_admin_only(self):
        # Owner admin can edit.
        self.assertTrue(can_edit_connection(self.owner_user, self.conn))
        # Non-owner (even an admin elsewhere) cannot.
        self.assertFalse(can_edit_connection(self.other_user, self.conn))
        # Superuser can.
        superuser = UserFactory(is_superuser=True)
        self.assertTrue(can_edit_connection(superuser, self.conn))

    def test_admin_workspaces_lists_admin_projects(self):
        names = {w.name for w in admin_workspaces(self.owner_user)}
        self.assertEqual(names, {"Owner WS", "Other WS"})


class ConnectionsPageSharingTests(TestCase):
    """The Connections page shows shared connections read-only to consumers."""

    def setUp(self):
        self.owner_ws = ProjectFactory(name="Owner WS")
        self.consumer_ws = ProjectFactory(name="Consumer WS")
        self.owner_user = UserFactory()
        self.consumer_user = UserFactory()
        MembershipFactory(user=self.owner_user, project=self.owner_ws, role="admin")
        MembershipFactory(user=self.consumer_user, project=self.consumer_ws, role="admin")
        self.conn = ModelConnectionFactory(
            project=self.owner_ws, name="Public OpenAI",
            description="Shared generously for the whole team",
            visibility=ModelConnection.Visibility.PUBLIC,
        )
        self.model = RegisteredModelFactory(connection=self.conn, project=self.owner_ws, display_name="GPT", model_id="gpt-4o")
        self.client = Client(SERVER_NAME="localhost")

    def _page_as(self, user, ws):
        _login(self.client, user)
        self.client.session["active_project_id"] = ws.id
        self.client.session.save()
        return self.client.get("/connections/")

    def test_consumer_sees_shared_connection_readonly(self):
        page = self._page_as(self.consumer_user, self.consumer_ws)
        self.assertContains(page, "Public OpenAI")
        self.assertContains(page, "Shared generously for the whole team")
        self.assertContains(page, "read-only")
        # No edit/delete affordances for the consumer.
        self.assertNotContains(page, f'data-conn-open="{self.conn.id}"')
        self.assertNotContains(page, f'data-discover="{self.conn.id}"')

    def test_owner_sees_edit_controls(self):
        page = self._page_as(self.owner_user, self.owner_ws)
        self.assertContains(page, f'data-conn-open="{self.conn.id}"')
        self.assertContains(page, f'data-discover="{self.conn.id}"')

    def test_consumer_cannot_edit_shared_connection(self):
        _login(self.client, self.consumer_user)
        self.client.session["active_project_id"] = self.consumer_ws.id
        self.client.session.save()
        before = self.conn.description
        resp = self.client.post("/connections/", {
            "action": "edit_connection", "conn_id": self.conn.id,
            "conn_name": "Hacked", "conn_base_url": "https://evil.example/v1",
            "conn_enabled": "1", "key_mode": "none",
        }, follow=True)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.description, before)   # unchanged
        self.assertEqual(self.conn.name, "Public OpenAI")  # unchanged
        self.assertContains(resp, "not found")

    def test_consumer_cannot_delete_shared_connection(self):
        _login(self.client, self.consumer_user)
        self.client.session["active_project_id"] = self.consumer_ws.id
        self.client.session.save()
        resp = self.client.post(f"/connections/{self.conn.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ModelConnection.objects.filter(pk=self.conn.id).exists())

    def test_owner_can_change_sharing_level(self):
        _login(self.client, self.owner_user)
        self.client.session["active_project_id"] = self.owner_ws.id
        self.client.session.save()
        self.client.post("/connections/", {
            "action": "edit_connection", "conn_id": self.conn.id,
            "conn_name": "Public OpenAI", "conn_base_url": "https://api.openai.com/v1",
            "conn_enabled": "1", "key_mode": "none",
            "conn_visibility": ModelConnection.Visibility.WORKSPACE,
        })
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.visibility, ModelConnection.Visibility.WORKSPACE)
        # The consumer no longer sees the connection *card* (a leftover success
        # message may still mention the name, so check the card anchor).
        page = self._page_as(self.consumer_user, self.consumer_ws)
        self.assertNotContains(page, f'id="conn-{self.conn.id}"')
        # The owner still sees their own connection.
        owner_page = self._page_as(self.owner_user, self.owner_ws)
        self.assertContains(owner_page, f'id="conn-{self.conn.id}"')


class PickerSharingTests(TestCase):
    """The New Experiment picker offers shared models with a sharing label."""

    def setUp(self):
        self.owner_ws = ProjectFactory(name="Owner WS")
        self.consumer_ws = ProjectFactory(name="Consumer WS")
        self.user = UserFactory()
        MembershipFactory(user=self.user, project=self.consumer_ws, role="auditor")
        self.conn = ModelConnectionFactory(
            project=self.owner_ws, name="Shared GPT",
            visibility=ModelConnection.Visibility.PUBLIC,
        )
        self.model = RegisteredModelFactory(connection=self.conn, project=self.owner_ws, display_name="GPT", model_id="gpt-4o")
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.user)
        self.client.session["active_project_id"] = self.consumer_ws.id
        self.client.session.save()

    def test_picker_lists_shared_model_with_label(self):
        page = self.client.get("/experiments/new/")
        self.assertContains(page, "Shared GPT")
        self.assertContains(page, "⇄ shared")
        # The model checkbox is present and usable.
        self.assertContains(page, f'value="{self.model.id}"')

    def test_private_connection_not_in_picker(self):
        private_conn = ModelConnectionFactory(
            project=self.owner_ws, name="Private Conn",
            visibility=ModelConnection.Visibility.WORKSPACE,
        )
        private_model = RegisteredModelFactory(connection=private_conn, project=self.owner_ws, display_name="Secret", model_id="secret")
        page = self.client.get("/experiments/new/")
        self.assertNotContains(page, "Private Conn")
        self.assertNotContains(page, f'value="{private_model.id}"')


class PingSharedConnectionTests(TestCase):
    """Pinging a shared connection is allowed for consumers; foreign ones 404."""

    def setUp(self):
        self.owner_ws = ProjectFactory(name="Owner WS")
        self.consumer_ws = ProjectFactory(name="Consumer WS")
        self.foreign_ws = ProjectFactory(name="Foreign WS")
        self.user = UserFactory()
        MembershipFactory(user=self.user, project=self.consumer_ws, role="admin")
        self.shared = ModelConnectionFactory(project=self.owner_ws, name="Shared", visibility=ModelConnection.Visibility.PUBLIC)
        self.foreign = ModelConnectionFactory(project=self.foreign_ws, name="Foreign", visibility=ModelConnection.Visibility.WORKSPACE)
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.user)
        self.client.session["active_project_id"] = self.consumer_ws.id
        self.client.session.save()

    def test_can_ping_shared_connection(self):
        fake = mock.MagicMock()
        fake.json.return_value = {"data": [{"id": "m1"}]}
        with mock.patch("model_registry.services.httpx.get", return_value=fake):
            resp = self.client.get(f"/api/models/ping-connection/{self.shared.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "up")

    def test_cannot_ping_foreign_private_connection(self):
        with mock.patch("model_registry.services.httpx.get") as get:
            resp = self.client.get(f"/api/models/ping-connection/{self.foreign.id}/")
        self.assertEqual(resp.status_code, 404)
        get.assert_not_called()
