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

    def test_page_requires_sign_in(self):
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_sidebar_links_to_chat(self):
        self._sign_in(username="nav")
        self.assertContains(self.client.get("/"), 'href="/chat/"')


@patch("chat.config.ENABLED", True)
class ChatOriginTests(TestCase):
    """The iframe must load the chat from the same host the page came from.

    Cookies are per host, not per port: a page served from 127.0.0.1 that embeds
    localhost:8801 sends the proxy no session cookie, and the frame bounces back
    to Studio — which embeds the frame again.
    """

    def setUp(self):
        self.client = Client()
        user = UserFactory(username="origin")
        MembershipFactory(user=user, project=ProjectFactory())
        self.client.force_login(user)

    def test_the_iframe_follows_the_host_in_the_address_bar(self):
        with patch("chat.config.PUBLIC_URL", ""), patch("chat.config.PROXY_PORT", 8801), \
             patch("chat.config.MODEL", ""):
            page = self.client.get("/chat/", HTTP_HOST="127.0.0.1:8000")
            self.assertContains(page, 'src="http://127.0.0.1:8801?temporary-chat=true"')
            page = self.client.get("/chat/", HTTP_HOST="localhost:8000")
            self.assertContains(page, 'src="http://localhost:8801?temporary-chat=true"')

    def test_an_explicit_chat_url_always_wins(self):
        with patch("chat.config.PUBLIC_URL", "https://chat.example.com"), \
             patch("chat.config.MODEL", ""):
            page = self.client.get("/chat/", HTTP_HOST="127.0.0.1:8000")
            self.assertContains(page, 'src="https://chat.example.com?temporary-chat=true"')


@patch("chat.config.ENABLED", True)
class ChatModelPinTests(TestCase):
    """The iframe URL carries ?model= so Open WebUI opens on the pinned model."""

    def setUp(self):
        self.client = Client()
        user = UserFactory(username="pin")
        MembershipFactory(user=user, project=ProjectFactory())
        self.client.force_login(user)

    def test_pinned_model_is_appended_to_the_iframe_url(self):
        with patch("chat.config.PUBLIC_URL", "http://127.0.0.1:8801"), \
             patch("chat.config.MODEL", "Qwen3.8-27B"):
            page = self.client.get("/chat/", HTTP_HOST="127.0.0.1:8000")
            # The & is HTML-escaped to &amp; in the rendered template.
            self.assertContains(
                page, 'src="http://127.0.0.1:8801?model=Qwen3.8-27B&amp;temporary-chat=true"')

    def test_no_model_param_when_no_model_is_pinned(self):
        with patch("chat.config.PUBLIC_URL", "http://127.0.0.1:8801"), \
             patch("chat.config.MODEL", ""):
            page = self.client.get("/chat/", HTTP_HOST="127.0.0.1:8000")
            self.assertContains(page, 'src="http://127.0.0.1:8801?temporary-chat=true"')
            self.assertNotContains(page, "model=")

    def test_the_iframe_is_always_forced_into_temporary_mode(self):
        # The embed is a throwaway surface: every chat must be temporary so
        # nothing accumulates in Open WebUI's history. The New Chat button is
        # hidden, so a fresh chat only ever starts from a full page load, which
        # re-reads this param.
        with patch("chat.config.PUBLIC_URL", "http://127.0.0.1:8801"), \
             patch("chat.config.MODEL", "Qwen3.8-27B"):
            page = self.client.get("/chat/", HTTP_HOST="127.0.0.1:8000")
            self.assertContains(page, "temporary-chat=true")
