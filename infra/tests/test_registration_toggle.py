"""Tests for the admin-controlled self-registration toggle (SiteSetting)."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from accounts.models import SiteSetting
from infra.tests.factories import UserFactory
from infra.tests.utils import login, patch_json, post_json, superuser

User = get_user_model()


class RegistrationToggleApiTest(TestCase):
    """POST /api/auth/register/ respects the registration_enabled setting."""

    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")

    def test_register_allowed_by_default(self):
        # No SiteSetting row -> default is True.
        resp = post_json(
            self.client,
            "/api/auth/register/",
            {"username": "carol", "email": "carol@example.com", "password": "another-strong-pass"},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(User.objects.filter(username="carol").exists())

    def test_register_blocked_when_disabled(self):
        SiteSetting.objects.create(key="registration_enabled", value=False)
        resp = post_json(
            self.client,
            "/api/auth/register/",
            {"username": "carol", "email": "carol@example.com", "password": "another-strong-pass"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "registration_disabled")
        self.assertFalse(User.objects.filter(username="carol").exists())


class AdminSiteSettingsApiTest(TestCase):
    def setUp(self):
        self.admin = superuser()
        self.client = Client(SERVER_NAME="localhost")
        login(self.client, self.admin)

    def test_requires_superuser(self):
        user = UserFactory()
        user.set_password("testpass123")
        user.save()
        login(self.client, user)
        resp = self.client.get("/api/admin/settings/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "super_admin_required")

    def test_get_defaults_to_enabled(self):
        resp = self.client.get("/api/admin/settings/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"registration_enabled": True})

    def test_patch_disables_and_reenables(self):
        resp = patch_json(self.client, "/api/admin/settings/", {"registration_enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["registration_enabled"], False)
        self.assertEqual(SiteSetting.objects.get(key="registration_enabled").value, False)

        resp = patch_json(self.client, "/api/admin/settings/", {"registration_enabled": True})
        self.assertIs(resp.json()["registration_enabled"], True)
        self.assertEqual(SiteSetting.objects.get(key="registration_enabled").value, True)


class WebRegistrationPageTest(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")

    def test_register_page_submits_by_default(self):
        resp = self.client.post(
            "/register/",
            {"username": "dave", "password": "another-strong-pass", "email": "dave@example.com"},
        )
        self.assertRedirects(resp, "/")
        self.assertTrue(User.objects.filter(username="dave").exists())

    def test_register_page_blocked_when_disabled(self):
        SiteSetting.objects.create(key="registration_enabled", value=False)
        resp = self.client.post(
            "/register/",
            {"username": "dave", "password": "another-strong-pass", "email": "dave@example.com"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Registration is currently disabled.")
        self.assertFalse(User.objects.filter(username="dave").exists())

    def test_login_hides_register_link_when_disabled(self):
        SiteSetting.objects.create(key="registration_enabled", value=False)
        resp = self.client.get("/login/")
        self.assertNotContains(resp, 'href="/register/"')

    def test_login_shows_register_link_by_default(self):
        resp = self.client.get("/login/")
        self.assertContains(resp, 'href="/register/"')


class AdminUsersTabToggleTest(TestCase):
    def test_users_tab_renders_toggle_state(self):
        admin = superuser()
        client = Client(SERVER_NAME="localhost")
        login(client, admin)

        resp = client.get("/admin-settings/?tab=users")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Allow self-registration")
        self.assertContains(resp, 'aria-checked="true"')

        SiteSetting.objects.create(key="registration_enabled", value=False)
        resp = client.get("/admin-settings/?tab=users")
        self.assertContains(resp, 'aria-checked="false"')
