"""The optional Open WebUI module: off by default, forward-auth when on.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test chat
"""
from unittest.mock import patch

from django.test import Client, TestCase

from accounts.models import ProjectMembership
from chat.config import EMAIL_HEADER, NAME_HEADER, ROLE_HEADER, is_disabled
from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory


class ChatSwitchTests(TestCase):
    def test_spellings_that_turn_chat_off(self):
        for value in (None, "", "off", "disabled", "DISABLED", " no ", "false", "0"):
            self.assertTrue(is_disabled(value), value)

    def test_spellings_that_turn_chat_on(self):
        for value in ("embedded", "docker", "on"):
            self.assertFalse(is_disabled(value), value)


class ChatDisabledTests(TestCase):
    def test_routes_404_when_off(self):
        user = UserFactory(username="off-user")
        MembershipFactory(user=user, project=ProjectFactory())
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get("/chat/").status_code, 404)
        self.assertEqual(client.get("/chat/authz").status_code, 404)

    def test_no_sidebar_entry_when_off(self):
        user = UserFactory(username="off-nav")
        MembershipFactory(user=user, project=ProjectFactory())
        client = Client()
        client.force_login(user)
        self.assertNotContains(client.get("/"), 'href="/chat/"')


@patch("chat.config.ENABLED", True)
class ChatEnabledTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.client = Client()

    def _sign_in(self, role=ProjectMembership.Role.VIEWER, **kwargs):
        user = UserFactory(**kwargs)
        MembershipFactory(user=user, project=self.project, role=role)
        self.client.force_login(user)
        return user

    def test_authz_rejects_anonymous(self):
        self.assertEqual(self.client.get("/chat/authz").status_code, 401)

    def test_authz_returns_identity_headers(self):
        self._sign_in(username="member", first_name="Ada", last_name="L")
        response = self.client.get("/chat/authz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response[EMAIL_HEADER], "member@test.com")
        self.assertEqual(response[NAME_HEADER], "Ada L")
        self.assertEqual(response[ROLE_HEADER], "user")

    def test_workspace_admin_is_chat_admin(self):
        self._sign_in(role=ProjectMembership.Role.ADMIN, username="boss")
        self.assertEqual(self.client.get("/chat/authz")[ROLE_HEADER], "admin")

    def test_superuser_is_chat_admin(self):
        self._sign_in(username="root", is_superuser=True)
        self.assertEqual(self.client.get("/chat/authz")[ROLE_HEADER], "admin")

    def test_user_without_email_still_gets_one(self):
        self._sign_in(username="anon", email="")
        self.assertEqual(self.client.get("/chat/authz")[EMAIL_HEADER], "anon@studio.local")

    def test_page_embeds_the_chat_origin(self):
        self._sign_in(username="viewer")
        with patch("chat.config.PUBLIC_URL", "http://localhost:8801"):
            response = self.client.get("/chat/")
        self.assertContains(response, 'src="http://localhost:8801"')

    def test_page_requires_sign_in(self):
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_sidebar_links_to_chat(self):
        self._sign_in(username="nav")
        self.assertContains(self.client.get("/"), 'href="/chat/"')
