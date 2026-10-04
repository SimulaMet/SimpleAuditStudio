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


@patch("chat.config.ENABLED", False)
class ChatDisabledTests(TestCase):
    """Patches ENABLED to False so these hold no matter what SIMPLEAUDIT_CHAT
    the surrounding environment (a dev .env loaded by manage.py) says."""

    def test_routes_404_when_off(self):
        user = UserFactory(username="off-user")
        MembershipFactory(user=user, project=ProjectFactory())
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get("/ai/").status_code, 404)
        self.assertEqual(client.get("/chat/authz").status_code, 404)

    def test_no_sidebar_entry_when_off(self):
        user = UserFactory(username="off-nav")
        MembershipFactory(user=user, project=ProjectFactory())
        client = Client()
        client.force_login(user)
        self.assertNotContains(client.get("/"), 'href="/ai/"')

    def test_chat_model_preference_rejected_when_off(self):
        user = UserFactory(username="off-pref")
        MembershipFactory(user=user, project=ProjectFactory())
        client = Client()
        client.force_login(user)
        resp = client.post("/me/preferences/", data='{"key": "chat_model", "value": "m"}',
                           content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        user.refresh_from_db()
        self.assertNotIn("chat_model", user.preferences)


@patch("chat.config.ENABLED", True)
class ChatPreferenceKeyTests(TestCase):
    """The chat app contributes its own preference key while it is on."""

    def setUp(self):
        self.user = UserFactory(username="on-pref")
        MembershipFactory(user=self.user, project=ProjectFactory())
        self.client = Client()
        self.client.force_login(self.user)

    def test_chat_model_preference_accepted_when_on(self):
        resp = self.client.post("/me/preferences/", data='{"key": "chat_model", "value": "m"}',
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences["chat_model"], "m")


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
        response = self.client.get("/ai/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_sidebar_links_to_chat(self):
        self._sign_in(username="nav")
        self.assertContains(self.client.get("/"), 'href="/ai/"')


@patch("chat.config.ENABLED", True)
class ChatOriginTests(TestCase):
    """The iframe loads the chat from the SAME origin as Studio.

    The subpath build serves Open WebUI at /chat/ on Studio's own origin, so
    the wrapper's iframe is same-origin: the session cookie rides along and
    Caddy does the forward-auth. No host/port juggling is needed anymore.
    """

    def setUp(self):
        self.client = Client()
        user = UserFactory(username="origin")
        MembershipFactory(user=user, project=ProjectFactory())
        self.client.force_login(user)

    def test_the_iframe_is_same_origin_on_any_host(self):
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        self.assertContains(page, 'src="/chat?temporary-chat=true"')
        page = self.client.get("/ai/", HTTP_HOST="localhost:8000")
        self.assertContains(page, 'src="/chat?temporary-chat=true"')

    def test_an_explicit_chat_url_always_wins(self):
        with patch("chat.config.PUBLIC_URL", "https://chat.example.com"):
            page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
            self.assertContains(page, 'src="https://chat.example.com?temporary-chat=true"')


@patch("chat.config.ENABLED", True)
class ChatModelPinTests(TestCase):
    """The iframe URL carries ?models= so Open WebUI opens on the pinned model.

    The default is the first model the user can see (no hardcoded model), so a
    user with a visible connection gets that model pinned; a user with none gets
    no pin at all.
    """

    def setUp(self):
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        self.client = Client()
        self.project = ProjectFactory()
        user = UserFactory(username="pin")
        MembershipFactory(user=user, project=self.project)
        self.client.force_login(user)
        # A connection serving a model, so the default resolves to a prefixed
        # id (the bare id alone is ambiguous across connections).
        self.conn = ModelConnectionFactory(project=self.project, name="OpenAI")
        RegisteredModelFactory(connection=self.conn, project=self.project,
                               display_name="Qwen", model_id="Qwen3.8-27B")

    def test_pinned_model_is_appended_to_the_iframe_url(self):
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        # The & is HTML-escaped to &amp; in the rendered template. The pin
        # is the prefixed id, namespaced by the connection that serves it.
        self.assertContains(
            page, f'src="/chat?models={self.conn.id}.Qwen3.8-27B&amp;temporary-chat=true"')

    def test_no_model_param_when_the_user_has_no_models(self):
        # A user with no visible connections has nothing to pin.
        from infra.tests.factories import UserFactory

        other = ProjectFactory()
        user = UserFactory(username="pin-empty")
        MembershipFactory(user=user, project=other)
        client = Client()
        client.force_login(user)
        page = client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        # The iframe src carries no models= (the picker's checkbox values
        # are value="...", not models=, so this is unambiguous).
        self.assertContains(page, 'src="/chat?temporary-chat=true"')
        self.assertNotContains(page, 'src="/chat?models=')

    def test_a_model_query_param_on_chat_is_ignored(self):
        # A hand-typed ?model= on /chat/ must not pin the chat — only the
        # /connections handoff (which validates the model) can.
        page = self.client.get("/ai/?model=gpt-4o", HTTP_HOST="127.0.0.1:8000")
        self.assertContains(
            page, f'src="/chat?models={self.conn.id}.Qwen3.8-27B&amp;temporary-chat=true"')
        self.assertNotContains(page, "models=gpt-4o")

    def test_the_iframe_is_always_forced_into_temporary_mode(self):
        # The embed is a throwaway surface: every chat must be temporary so
        # nothing accumulates in Open WebUI's history. The New Chat button is
        # hidden, so a fresh chat only ever starts from a full page load, which
        # re-reads this param.
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        self.assertContains(page, "temporary-chat=true")


@patch("chat.config.ENABLED", True)
class ChatWithHandoffTests(TestCase):
    """/ai/with/<connection_id>/<model_id> validates the model, stashes it in
    the session, and redirects to /ai/ — where it is consumed exactly once."""

    def setUp(self):
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        self.project = ProjectFactory()
        user = UserFactory(username="handoff")
        MembershipFactory(user=user, project=self.project)
        self.client = Client()
        self.client.force_login(user)
        conn = ModelConnectionFactory(project=self.project, name="OpenAI",
                                       base_url="http://localhost:9999/v1")
        self.model = RegisteredModelFactory(connection=conn, project=self.project,
                                             display_name="GPT", model_id="gpt-4o")
        # A second model that sorts before "GPT", so the default (first
        # available) differs from the handoff pin — the refresh assertion below
        # can then tell the one-shot pin apart from the fallback.
        self.default_model = RegisteredModelFactory(
            connection=conn, project=self.project,
            display_name="Alpha", model_id="alpha-1")

    def test_handoff_pins_the_model_for_one_load(self):
        resp = self.client.get(f"/ai/with/{self.model.connection_id}/{self.model.model_id}")
        # fetch_redirect_response=False so the redirect isn't followed here
        # (following it would consume the one-shot session value).
        self.assertRedirects(resp, "/ai/", fetch_redirect_response=False)
        page = self.client.get("/ai/")
        # The pin is the prefixed id (connection id + bare model id).
        self.assertContains(
            page, f'src="/chat?models={self.model.connection_id}.gpt-4o&amp;temporary-chat=true"')
        # Consumed: a refresh no longer carries the handoff pin — it falls
        # back to the default (the first available model, alpha-1 here).
        page2 = self.client.get("/ai/")
        self.assertNotContains(page2, f"?models={self.model.connection_id}.gpt-4o")
        self.assertContains(
            page2, f'src="/chat?models={self.default_model.connection_id}.alpha-1&amp;temporary-chat=true"')

    def test_handoff_ignores_a_model_the_user_cannot_see(self):
        resp = self.client.get(f"/ai/with/{self.model.connection_id}/does-not-exist")
        self.assertRedirects(resp, "/ai/", fetch_redirect_response=False)
        page = self.client.get("/ai/")
        # The invalid model is never pinned (the default may still be).
        self.assertNotContains(page, "models=does-not-exist")

    def test_handoff_ignores_a_model_on_a_connection_the_user_cannot_see(self):
        # The same bare model id exists on a connection in another workspace;
        # pointing the handoff at that connection must not pin it.
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        other = ProjectFactory()
        other_conn = ModelConnectionFactory(project=other, name="Other")
        RegisteredModelFactory(connection=other_conn, project=other,
                               display_name="GPT", model_id="gpt-4o")
        resp = self.client.get(f"/ai/with/{other_conn.id}/{self.model.model_id}")
        self.assertRedirects(resp, "/ai/", fetch_redirect_response=False)
        page = self.client.get("/ai/")
        # The iframe never carries the other connection's prefixed id.
        self.assertNotContains(page, f"?models={other_conn.id}.gpt-4o")


@patch("chat.config.ENABLED", True)
class ChatModelPickerTests(TestCase):
    """The top-bar picker offers the user's visible models, grouped by
    connection, and pre-checks the pinned default(s)."""

    def setUp(self):
        self.project = ProjectFactory()
        self.client = Client()
        user = UserFactory(username="picker")
        MembershipFactory(user=user, project=self.project)
        self.client.force_login(user)

    def test_visible_models_are_listed_and_default_is_checked(self):
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        conn = ModelConnectionFactory(project=self.project, name="OpenAI")
        RegisteredModelFactory(connection=conn, project=self.project,
                               display_name="Qwen", model_id="Qwen3.8-27B")
        RegisteredModelFactory(connection=conn, project=self.project,
                               display_name="GPT", model_id="gpt-4o")
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        # Checkbox values are the prefixed ids (connection id + bare model id).
        # The default is the first available model, ordered by display name, so
        # "GPT" (gpt-4o) sorts before "Qwen" and is pre-checked.
        self.assertContains(page, f'value="{conn.id}.gpt-4o" checked')
        self.assertContains(page, f'value="{conn.id}.Qwen3.8-27B"')
        self.assertNotContains(page, f'value="{conn.id}.Qwen3.8-27B" checked')
        self.assertContains(page, ">OpenAI<")

    def test_models_from_other_workspaces_are_hidden(self):
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        other = ProjectFactory()
        conn = ModelConnectionFactory(project=other, name="Secret")
        RegisteredModelFactory(connection=conn, project=other,
                               display_name="Hidden", model_id="hidden-model")
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        self.assertNotContains(page, "hidden-model")
        self.assertContains(page, "No models yet")

    def test_disabled_connection_is_not_offered(self):
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        conn = ModelConnectionFactory(project=self.project, name="Off", enabled=False)
        RegisteredModelFactory(connection=conn, project=self.project,
                               display_name="Dead", model_id="dead-model")
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        self.assertNotContains(page, "dead-model")

    def test_search_box_and_backdrop_are_rendered(self):
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        conn = ModelConnectionFactory(project=self.project, name="OpenAI")
        RegisteredModelFactory(connection=conn, project=self.project,
                               display_name="Qwen", model_id="Qwen3.8-27B")
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        # The filter input and the click-outside backdrop are present.
        self.assertContains(page, 'id="model-picker-search"')
        self.assertContains(page, 'id="picker-backdrop"')
        self.assertContains(page, 'id="model-picker-list"')


@patch("chat.config.ENABLED", True)
class ChatNoModelsTests(TestCase):
    """A user with no project membership (request.project is None) or no
    visible models gets a friendly "no models" state, not a broken empty page."""

    def setUp(self):
        self.client = Client()

    def _sign_in_no_project(self, username="nomember"):
        # No MembershipFactory: the user has no project at all, so
        # ProjectMiddleware leaves request.project as None.
        user = UserFactory(username=username)
        self.client.force_login(user)
        return user

    def test_no_project_membership_shows_no_models_state(self):
        self._sign_in_no_project()
        page = self.client.get("/ai/")
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["has_models"])
        self.assertIn("no_models_message", page.context)
        # The friendly state is rendered, and the picker/iframe are not.
        self.assertContains(page, "No models available")
        self.assertContains(page, "no-models")
        self.assertNotContains(page, 'id="model-picker"')
        self.assertNotContains(page, 'id="chat-frame"')

    def test_user_with_visible_models_still_gets_the_picker(self):
        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        project = ProjectFactory()
        user = UserFactory(username="hasmodels")
        MembershipFactory(user=user, project=project)
        self.client.force_login(user)
        conn = ModelConnectionFactory(project=project, name="OpenAI")
        RegisteredModelFactory(connection=conn, project=project,
                               display_name="Qwen", model_id="Qwen3.8-27B")
        page = self.client.get("/ai/", HTTP_HOST="127.0.0.1:8000")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["has_models"])
        self.assertNotIn("no_models_message", page.context)
        # The normal picker and iframe render as before.
        self.assertContains(page, 'id="model-picker"')
        self.assertContains(page, 'id="chat-frame"')
        # The single model is the default, so its prefixed id is pre-checked.
        self.assertContains(page, f'value="{conn.id}.Qwen3.8-27B" checked')
        self.assertNotContains(page, "No models available")

    def test_handoff_without_a_project_redirects_to_chat(self):
        # No project membership: the model can't be seen, so the handoff must
        # not pin it and must simply redirect to /chat/ (no 500, no 404).
        self._sign_in_no_project(username="handoff-noproj")
        resp = self.client.get("/ai/with/1/gpt-4o")
        self.assertRedirects(resp, "/ai/", fetch_redirect_response=False)
        # And the chat page itself degrades gracefully rather than erroring.
        self.assertEqual(self.client.get("/ai/").status_code, 200)


@patch("chat.config.ENABLED", True)
class ChatCssMaskTests(TestCase):
    """/chat/css-mask: the session-aware embed stylesheet Caddy proxies to."""

    def setUp(self):
        self.project = ProjectFactory()
        self.client = Client()

    def test_anonymous_gets_the_plain_embed_css(self):
        from pathlib import Path

        resp = self.client.get("/chat/css-mask")
        self.assertEqual(resp.status_code, 200)
        expected = (Path(__file__).resolve().parents[1] / "embed.css").read_bytes()
        self.assertEqual(resp.content, expected)
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertEqual(resp["Content-Type"], "text/css")

    def test_embed_admin_query_stamps_the_session_and_serves_admin_css(self):
        # The admin pages' iframe URL loads /chat/authz?embed=admin first ...
        self._admin = UserFactory(username="css-admin")
        MembershipFactory(user=self._admin, project=self.project,
                          role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self._admin)
        self.assertEqual(self.client.get("/chat/authz?embed=admin").status_code, 200)
        # ... and the /static/custom.css load that follows (no query) gets the
        # admin skin.
        resp = self.client.get("/chat/css-mask")
        from pathlib import Path
        expected = (Path(__file__).resolve().parents[1] / "embed_admin.css").read_bytes()
        self.assertEqual(resp.content, expected)

    def test_regular_user_gets_plain_css_even_with_stale_flag(self):
        user = UserFactory(username="css-plain")
        MembershipFactory(user=user, project=self.project,
                          role=ProjectMembership.Role.VIEWER)
        self.client.force_login(user)
        # A flag from a previous (other) session is not enough once expired.
        self.client.session["studio_chat_embed_admin"] = 1.0
        self.client.session.save()
        from pathlib import Path
        resp = self.client.get("/chat/css-mask")
        expected = (Path(__file__).resolve().parents[1] / "embed.css").read_bytes()
        self.assertEqual(resp.content, expected)
