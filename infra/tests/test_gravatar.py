"""Gravatar avatars are opt-in: off by default, no gravatar.com URL is rendered."""
from django.contrib.auth.models import AnonymousUser
from django.test import Client, TestCase, override_settings

from infra.context_processors import gravatar_for
from infra.tests.factories import UserFactory


class GravatarSettingTest(TestCase):
    def setUp(self):
        self.user = UserFactory(username="jane", email="Jane@X.com")
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def test_off_by_default(self):
        self.assertIsNone(gravatar_for(self.user))
        content = self.client.get("/profile/").content.decode()
        self.assertNotIn("gravatar.com/avatar/", content)

    @override_settings(SIMPLEAUDIT_GRAVATAR=True)
    def test_on_renders_the_gravatar_url(self):
        # MD5 of the trimmed, lower-cased email "jane@x.com".
        url = "https://www.gravatar.com/avatar/8e747d19e83ff063fbb509e348a296d4"
        self.assertTrue(gravatar_for(self.user).startswith(url))
        content = self.client.get("/profile/").content.decode()
        self.assertIn(url, content)

    @override_settings(SIMPLEAUDIT_GRAVATAR=True)
    def test_on_still_none_without_a_signed_in_user_with_email(self):
        self.assertIsNone(gravatar_for(AnonymousUser()))
        self.assertIsNone(gravatar_for(UserFactory(username="noemail", email="")))
