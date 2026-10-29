"""Tests for Studio\'s OpenAI-compatible model API (issue #24).

Covers the personal API-key credential service, the machine-to-machine
endpoints (/api/v1/models, /api/v1/chat/completions), and the session-auth key
issue/revoke views.
"""
import json
from unittest import mock

from django.test import Client, TestCase
from rest_framework.test import APIClient
from simpleaudit.tracing.auth import hash_secret, new_salt, token_lookup_prefix

from infra.tests.factories import (
    AgentFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)
from model_registry import api_credential_services as api_cred
from model_registry.models import ApiCredential


def _fixed_token():
    return "sa_api_test_fixed_token_0123456789abcdef"


def _make_key(user, project, token=None):
    """Create an ApiCredential with a known token and return it."""
    token = token or _fixed_token()
    salt = new_salt()
    ApiCredential.objects.create(
        project=project, user=user, salt=salt,
        secret_hash=hash_secret(token, salt), token_prefix=token_lookup_prefix(token),
    )
    return token


# --- credential service ------------------------------------------------------

class ApiCredentialServiceTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()

    def test_upsert_returns_token_and_stores_hash(self):
        nc = api_cred.upsert_credential(project=self.project, user=self.user)
        self.assertTrue(nc.secret.startswith("sa_api_"))
        cred = ApiCredential.objects.get(user=self.user)
        self.assertNotEqual(cred.secret_hash, nc.secret)
        self.assertEqual(cred.token_prefix, token_lookup_prefix(nc.secret))

    def test_upsert_is_one_per_user_and_rotates(self):
        first = api_cred.upsert_credential(project=self.project, user=self.user)
        second = api_cred.upsert_credential(project=self.project, user=self.user)
        self.assertNotEqual(first.secret, second.secret)
        self.assertEqual(ApiCredential.objects.filter(user=self.user).count(), 1)
        self.assertIsNotNone(api_cred.verify_bearer(second.secret))
        self.assertIsNone(api_cred.verify_bearer(first.secret))

    def test_revoke_disables_key(self):
        tok = api_cred.upsert_credential(project=self.project, user=self.user).secret
        revoked = api_cred.revoke_credential(user=self.user)
        self.assertTrue(revoked)
        cred = ApiCredential.objects.get(user=self.user)
        self.assertFalse(cred.enabled)
        # A revoked key no longer authenticates.
        self.assertIsNone(api_cred.verify_bearer(tok))

    def test_verify_bearer_matches_by_prefix_then_hash(self):
        tok = api_cred.upsert_credential(project=self.project, user=self.user).secret
        self.assertEqual(api_cred.verify_bearer(tok).user_id, self.user.id)
        self.assertIsNone(api_cred.verify_bearer("sa_api_" + "x" * 32))

    def test_project_scoping(self):
        other = ProjectFactory()
        tok = api_cred.upsert_credential(project=self.project, user=self.user).secret
        self.assertIsNotNone(api_cred.verify_bearer(tok, project=self.project))
        self.assertIsNone(api_cred.verify_bearer(tok, project=other))

    def test_api_token_distinct_from_otlp_space(self):
        from model_registry.otlp_services import generate_token
        otlp_token = generate_token()  # "sa_otlp_..."
        api_token = api_cred.upsert_credential(project=self.project, user=self.user).secret
        self.assertTrue(otlp_token.startswith("sa_otlp_"))
        self.assertTrue(api_token.startswith("sa_api_"))
        self.assertNotEqual(otlp_token[:8], api_token[:8])


# --- GET /api/v1/models ------------------------------------------------------

class ApiModelsEndpointTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="owui-a")
        RegisteredModelFactory(connection=self.conn, model_id="gpt-4o-mini", display_name="GPT-4o mini")
        self.tok = _make_key(self.user, self.project)
        self.client = Client(SERVER_NAME="localhost")

    @mock.patch("chat.config.ENABLED", True)
    def test_lists_visible_models_with_prefixed_ids(self):
        resp = self.client.get("/api/v1/models", HTTP_AUTHORIZATION=f"Bearer {self.tok}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["object"], "list")
        ids = [m["id"] for m in body["data"]]
        self.assertIn(f"{self.conn.id}.gpt-4o-mini", ids)

    @mock.patch("chat.config.ENABLED", True)
    def test_bad_key_401(self):
        resp = self.client.get("/api/v1/models", HTTP_AUTHORIZATION="Bearer sa_api_wrongwrongwrong000000000000000")
        self.assertEqual(resp.status_code, 401)

    @mock.patch("chat.config.ENABLED", True)
    def test_no_auth_401(self):
        self.assertEqual(self.client.get("/api/v1/models").status_code, 401)

    @mock.patch("chat.config.ENABLED", False)
    def test_chat_disabled_404(self):
        resp = self.client.get("/api/v1/models", HTTP_AUTHORIZATION=f"Bearer {self.tok}")
        self.assertEqual(resp.status_code, 404)

    @mock.patch("chat.config.ENABLED", True)
    def test_only_enabled_models_listed(self):
        off = RegisteredModelFactory(connection=self.conn, model_id="disabled-model")
        off.enabled = False
        off.save()
        resp = self.client.get("/api/v1/models", HTTP_AUTHORIZATION=f"Bearer {self.tok}")
        ids = [m["id"] for m in resp.json()["data"]]
        self.assertNotIn(f"{self.conn.id}.disabled-model", ids)


# --- POST /api/v1/chat/completions -------------------------------------------

class ApiChatCompletionsEndpointTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="owui-a")
        self.model = RegisteredModelFactory(connection=self.conn, model_id="gpt-4o-mini")
        self.tok = _make_key(self.user, self.project)
        self.client = Client(SERVER_NAME="localhost")
        self.model_id = f"{self.conn.id}.gpt-4o-mini"

    def _post(self, payload, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            "/api/v1/chat/completions", data=json.dumps(payload),
            content_type="application/json", **headers,
        )

    @mock.patch("chat.api.ChatAPI.chat_completion")
    @mock.patch("chat.config.ENABLED", True)
    def test_valid_request_proxies_to_owui_as_user(self, mock_cc):
        mock_cc.return_value = {"id": "cmpl_1", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        resp = self._post({"model": self.model_id, "messages": [{"role": "user", "content": "hello"}]}, token=self.tok)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], "cmpl_1")
        args, kwargs = mock_cc.call_args
        sent = args[0] if args else kwargs.get("payload")
        self.assertEqual(sent["model"], self.model_id)
        self.assertEqual(sent["messages"][0]["content"], "hello")
        # Passthrough forces a non-streaming request upstream.
        self.assertFalse(sent.get("stream"))

    @mock.patch("chat.config.ENABLED", True)
    def test_missing_messages_400(self):
        resp = self._post({"model": self.model_id}, token=self.tok)
        self.assertEqual(resp.status_code, 400)

    @mock.patch("chat.config.ENABLED", True)
    def test_unknown_model_404(self):
        resp = self._post({"model": "nope.nope", "messages": [{"role": "user", "content": "x"}]}, token=self.tok)
        self.assertEqual(resp.status_code, 404)

    @mock.patch("chat.config.ENABLED", True)
    def test_malformed_model_id_404(self):
        resp = self._post({"model": "just-an-id", "messages": [{"role": "user", "content": "x"}]}, token=self.tok)
        self.assertEqual(resp.status_code, 404)

    @mock.patch("chat.config.ENABLED", True)
    def test_other_users_connection_404(self):
        stranger = UserFactory()
        p2 = ProjectFactory()
        MembershipFactory(user=stranger, project=p2, role="admin")
        c2 = ModelConnectionFactory(project=p2, name="owui-b")
        RegisteredModelFactory(connection=c2, model_id="gpt-4o-mini")
        resp = self._post({"model": f"{c2.id}.gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]}, token=self.tok)
        self.assertEqual(resp.status_code, 404)

    @mock.patch("chat.api.ChatAPI.chat_completion")
    @mock.patch("chat.config.ENABLED", True)
    def test_upstream_error_502(self, mock_cc):
        from chat.api import ChatAPIError
        mock_cc.side_effect = ChatAPIError("boom")
        resp = self._post({"model": self.model_id, "messages": [{"role": "user", "content": "x"}]}, token=self.tok)
        self.assertEqual(resp.status_code, 502)


# --- agents exposed as models ------------------------------------------------

class AgentPassthroughTest(TestCase):
    """Synced agents are callable through the same OpenAI-compatible surface."""

    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="owui-a")
        RegisteredModelFactory(connection=self.conn, model_id="gpt-4o-mini")
        self.agent = AgentFactory(project=self.project, external_id="studio.agent-77")
        self.tok = _make_key(self.user, self.project)
        self.client = Client(SERVER_NAME="localhost")

    @mock.patch("chat.config.ENABLED", True)
    def test_agent_id_listed_in_models(self):
        resp = self.client.get("/api/v1/models", HTTP_AUTHORIZATION=f"Bearer {self.tok}")
        ids = [m["id"] for m in resp.json()["data"]]
        self.assertIn("studio.agent-77", ids)

    @mock.patch("chat.api.ChatAPI.chat_completion")
    @mock.patch("chat.config.ENABLED", True)
    def test_agent_completion_proxies_with_agent_id(self, mock_cc):
        mock_cc.return_value = {"id": "cmpl_a", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        resp = self.client.post(
            "/api/v1/chat/completions",
            data=json.dumps({"model": "studio.agent-77", "messages": [{"role": "user", "content": "hello"}]}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.tok}",
        )
        self.assertEqual(resp.status_code, 200)
        sent = mock_cc.call_args[0][0]
        self.assertEqual(sent["model"], "studio.agent-77")

    @mock.patch("chat.config.ENABLED", True)
    def test_unknown_agent_404(self):
        resp = self.client.post(
            "/api/v1/chat/completions",
            data=json.dumps({"model": "studio.agent-999", "messages": [{"role": "user", "content": "x"}]}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.tok}",
        )
        self.assertEqual(resp.status_code, 404)

    @mock.patch("chat.config.ENABLED", True)
    def test_other_project_agent_404(self):
        p2 = ProjectFactory()
        AgentFactory(project=p2, external_id="studio.agent-88")
        resp = self.client.post(
            "/api/v1/chat/completions",
            data=json.dumps({"model": "studio.agent-88", "messages": [{"role": "user", "content": "x"}]}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.tok}",
        )
        self.assertEqual(resp.status_code, 404)


# --- Expose UI on the agent detail page -------------------------------------

class AgentExposeUiTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = UserFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="owui-a")
        RegisteredModelFactory(connection=self.conn, model_id="gpt-4o-mini")
        self.agent = AgentFactory(project=self.project, external_id="studio.agent-77")
        self.client.force_login(self.user)

    @mock.patch("chat.config.ENABLED", True)
    def test_expose_controls_render_for_synced_agent(self):
        resp = self.client.get(f"/agents/{self.agent.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('data-expose', html)
        self.assertIn("expose-dialog", html)
        self.assertIn("studio.agent-77", html)

    @mock.patch("chat.config.ENABLED", True)
    def test_no_expose_before_first_sync(self):
        unsynced = AgentFactory(project=self.project, external_id="")
        resp = self.client.get(f"/agents/{unsynced.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("data-expose", resp.content.decode())


# --- key management (session auth) ------------------------------------------

class ApiKeyManagementTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = APIClient()
        self.client.force_login(self.user)

    @mock.patch("chat.config.ENABLED", True)
    def test_issue_creates_key(self):
        resp = self.client.post("/api/v1/keys/issue/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["token"].startswith("sa_api_"))
        self.assertTrue(ApiCredential.objects.filter(user=self.user).exists())

    @mock.patch("chat.config.ENABLED", True)
    def test_issue_rotates_existing(self):
        first = self.client.post("/api/v1/keys/issue/").json()["token"]
        second = self.client.post("/api/v1/keys/issue/").json()["token"]
        self.assertNotEqual(first, second)
        self.assertEqual(ApiCredential.objects.filter(user=self.user).count(), 1)

    @mock.patch("chat.config.ENABLED", False)
    def test_issue_requires_chat(self):
        resp = self.client.post("/api/v1/keys/issue/")
        self.assertEqual(resp.status_code, 404)

    @mock.patch("chat.config.ENABLED", True)
    def test_revoke(self):
        self.client.post("/api/v1/keys/issue/")
        resp = self.client.post("/api/v1/keys/revoke/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(ApiCredential.objects.get(user=self.user))
        self.assertFalse(ApiCredential.objects.get(user=self.user).enabled)

    def test_unauthenticated_forbidden(self):
        # DRF session auth returns 403 (not 401) for an anonymous caller.
        anon = APIClient()
        self.assertIn(
            anon.post("/api/v1/keys/issue/", HTTP_AUTHORIZATION="").status_code,
            (401, 403),
        )
