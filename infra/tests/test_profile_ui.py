"""UI tests for the Profile page (server-rendered) and sidebar navigation."""
from django.test import Client, TestCase

from infra.tests.factories import UserFactory


class ProfilePageTest(TestCase):
    def setUp(self):
        self.user = UserFactory(username="jane", email="jane@x.com")
        self.user.set_password("testpass123")
        self.user.save()
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def test_local_user_sees_change_password_form(self):
        resp = self.client.get("/profile/")
        content = resp.content.decode()
        self.assertIn("Change password", content)
        self.assertIn('name="current_password"', content)
        self.assertNotIn("Set a password", content)

    def test_sso_user_sees_set_password_form(self):
        sso = UserFactory(username="ssojane", email="ssojane@x.com", workos_user_id="wo_123")
        c = Client(SERVER_NAME="localhost")
        c.force_login(sso)
        content = c.get("/profile/").content.decode()
        self.assertIn("Set a password", content)
        self.assertNotIn('name="current_password"', content)
        self.assertIn("WorkOS", content)

    def test_sso_user_can_set_password_via_ui(self):
        sso = UserFactory(username="ssojane2", email="ssojane2@x.com", workos_user_id="wo_456")
        c = Client(SERVER_NAME="localhost")
        c.force_login(sso)
        resp = c.post("/profile/", {"form": "password", "new_password": "N3w-pass-456"})
        self.assertEqual(resp.status_code, 302)
        sso.refresh_from_db()
        self.assertTrue(sso.check_password("N3w-pass-456"))

    def test_local_user_wrong_current_password_shows_error(self):
        resp = self.client.post(
            "/profile/", {"form": "password", "current_password": "wrong", "new_password": "N3w-pass-456"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Current password is incorrect.", resp.content.decode())

    def test_blank_new_password_rejected(self):
        resp = self.client.post("/profile/", {"form": "password", "current_password": "testpass123", "new_password": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Enter a new password.", resp.content.decode())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("testpass123"))


class SidebarNavTest(TestCase):
    def setUp(self):
        self.user = UserFactory(username="jane", email="jane@x.com")
        self.user.set_password("testpass123")
        self.user.save()
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def _dashboard(self):
        return self.client.get("/dashboard/").content.decode()

    def test_no_dedicated_workspaces_or_profile_nav_links(self):
        content = self._dashboard()
        # The dedicated nav links are gone (icon + label pair).
        self.assertNotIn("sidebar-label\">Workspaces</span>", content)
        self.assertNotIn("sidebar-label\">Profile</span>", content)

    def test_picker_dropdown_has_manage_workspaces_link(self):
        content = self._dashboard()
        self.assertIn("Manage workspaces", content)
        self.assertIn('href="/workspaces/"', content)

    def test_bottom_username_links_to_profile(self):
        content = self._dashboard()
        # The avatar/username row is now a link to /profile/.
        self.assertIn('href="/profile/"', content)
        self.assertIn('title="Profile"', content)

    def test_logout_still_present(self):
        content = self._dashboard()
        self.assertIn('href="/logout/"', content)
