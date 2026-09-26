"""Tests for the self-service Profile API (/api/auth/profile/)."""
import json

from django.test import Client, TestCase

from infra.tests.factories import UserFactory


def _login(client, user, pw="testpass123"):
    creds = {"username": user.username, "password": pw}
    client.login(**creds)


def _patch(client, url, payload):
    return client.patch(url, data=json.dumps(payload), content_type="application/json")


class ProfileApiTest(TestCase):
    def setUp(self):
        self.user = UserFactory(username="jane", email="jane@old.com")
        self.user.set_password("testpass123")
        self.user.save()
        self.client = Client(SERVER_NAME="localhost")
        _login(self.client, self.user)

    def test_requires_auth(self):
        anon = Client(SERVER_NAME="localhost")
        resp = anon.patch("/api/auth/profile/", data=json.dumps({"first_name": "X"}), content_type="application/json")
        self.assertIn(resp.status_code, (401, 403))

    def test_update_name_and_email(self):
        resp = _patch(self.client, "/api/auth/profile/", {"first_name": "Jane", "last_name": "Doe", "email": "jane@new.com"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Jane")
        self.assertEqual(self.user.last_name, "Doe")
        self.assertEqual(self.user.email, "jane@new.com")

    def test_update_username(self):
        resp = _patch(self.client, "/api/auth/profile/", {"username": "jane2"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "jane2")

    def test_username_taken_rejected(self):
        UserFactory(username="taken")
        resp = _patch(self.client, "/api/auth/profile/", {"username": "taken"})
        self.assertEqual(resp.status_code, 400)

    def test_email_taken_rejected(self):
        UserFactory(username="other", email="dup@test.com")
        resp = _patch(self.client, "/api/auth/profile/", {"email": "dup@test.com"})
        self.assertEqual(resp.status_code, 400)

    def test_password_change_success(self):
        resp = _patch(self.client, "/api/auth/profile/", {"current_password": "testpass123", "new_password": "N3w-pass-456"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-pass-456"))
        self.assertFalse(self.user.check_password("testpass123"))

    def test_password_change_wrong_current_rejected(self):
        resp = _patch(self.client, "/api/auth/profile/", {"current_password": "wrong-pass", "new_password": "N3w-pass-456"})
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("testpass123"))

    def test_password_change_requires_current(self):
        resp = _patch(self.client, "/api/auth/profile/", {"new_password": "N3w-pass-456"})
        self.assertEqual(resp.status_code, 400)

    def test_cannot_change_superuser_flag(self):
        # is_superuser is not a field on the self-service serializer.
        resp = _patch(self.client, "/api/auth/profile/", {"is_superuser": True})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)


class SsoSetPasswordApiTest(TestCase):
    """WorkOS-provisioned users have no local password; they must be able to
    set one without supplying a current password."""

    def setUp(self):
        # Simulate a WorkOS-provisioned account: workos_user_id set, no password.
        self.user = UserFactory(username="ssojane", email="ssojane@x.com", workos_user_id="wo_123")
        self.client = Client(SERVER_NAME="localhost")

    def _login_sso(self):
        # WorkOS login authenticates a user whose password is empty, so the
        # session hash matches the empty password. force_login reproduces that.
        self.client.force_login(self.user)

    def test_sso_user_can_set_password_without_current(self):
        self._login_sso()
        resp = _patch(self.client, "/api/auth/profile/", {"new_password": "N3w-pass-456"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3w-pass-456"))

    def test_sso_user_set_password_validates_strength(self):
        self._login_sso()
        resp = _patch(self.client, "/api/auth/profile/", {"new_password": "short"})
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, "")  # not set

    def test_local_user_still_requires_current_password(self):
        # A user who already has a local password must still prove it.
        self.user.set_password("testpass123")
        self.user.save()
        self.client.force_login(self.user)
        resp = _patch(self.client, "/api/auth/profile/", {"new_password": "N3w-pass-456"})
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("testpass123"))
