"""Admin-only extensions vs. a self-created workspace (follow-up to issue #83)."""
import json

from django.test import Client, TestCase, override_settings

from accounts.services import create_workspace
from infra.tests.factories import UserFactory

ADMIN_ONLY = [{"slug": "feedback", "label": "Feedback", "section": "Tools", "embed": "/feedback/"}]   # roles default ["admin"]


@override_settings(SIMPLEAUDIT_EXTENSIONS=json.dumps(ADMIN_ONLY))
class SelfCreatedWorkspaceTests(TestCase):
    def _assert_no_admin_extension(self, client):
        authz = client.get("/ext/authz?role=admin")
        seen = {
            "menu_entry_in_html": 'href="/x/feedback/"' in client.get("/").content.decode(),
            "embed_status": client.get("/x/feedback/").status_code,
            "authz_status": authz.status_code,
            "authz_role_header": authz.get("X-Studio-Role"),
        }
        self.assertEqual(seen, {"menu_entry_in_html": False, "embed_status": 404,
                                "authz_status": 403, "authz_role_header": None})

    def test_via_service(self):
        user = UserFactory()
        client = Client()
        client.force_login(user)
        self._assert_no_admin_extension(client)        # baseline: passes
        create_workspace(user=user, name="mine")
        self._assert_no_admin_extension(client)        # fails on ba55a23

    def test_via_api(self):
        user = UserFactory()
        client = Client()
        client.force_login(user)
        self.assertEqual(client.post("/api/projects/create/", {"name": "mine"}).status_code, 201)
        self._assert_no_admin_extension(client)

    def test_via_self_registration(self):
        client = Client()
        resp = client.post("/api/auth/register/", {
            "username": "stranger", "email": "stranger@example.org", "password": "x-Correct-Horse-9",
        })
        self.assertEqual(resp.status_code, 201)          # signed in, no invitation
        self.assertEqual(client.post("/api/projects/create/", {"name": "mine"}).status_code, 201)
        self._assert_no_admin_extension(client)
