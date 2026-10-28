"""Model metadata discovery is an optional, bounded extension of /v1/models."""
from types import SimpleNamespace
from unittest import mock

import httpx
from django.test import SimpleTestCase

from audits.target_metadata import flattened_metadata
from model_registry.remote_metadata import discover_target_metadata


def model():
    return SimpleNamespace(
        model_id="team/my-agent",
        connection=SimpleNamespace(
            provider="custom", base_url="https://example.invalid/v1",
            secret_reference="", api_key_direct="test-key"
        ),
    )


def response(payload, status=200):
    r = httpx.Response(status, json=payload, request=httpx.Request("GET", "https://example.invalid/v1/models"))
    return r


class RemoteMetadataDiscoveryTests(SimpleTestCase):
    @mock.patch("model_registry.remote_metadata.httpx.get")
    def test_nested_json_from_detail_is_preserved_with_authorization(self, get):
        metadata = {
            "version": "2.1", "prompts": {"retrieval": {"content": "Retrieve {query}", "version": "3"}},
            "retrieval": {"top_k": 5, "enabled": False}, "tools": ["search", "calculator"],
        }
        get.return_value = response({"id": "team/my-agent", "object": "model", "metadata": metadata})
        result = discover_target_metadata(model())
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["metadata"], metadata)
        self.assertEqual(result["source"], "models.retrieve")
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith("/models/team%2Fmy-agent"))
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], ("Bea" + "rer ") + "test-key")
        self.assertNotIn("test-key", str(result))

    @mock.patch("model_registry.remote_metadata.httpx.get")
    def test_list_fallback_when_detail_404(self, get):
        get.side_effect = [
            response({"error": "not found"}, status=404),
            response({"object": "list", "data": [
                {"id": "other", "metadata": {"no": "ignore"}},
                {"id": "team/my-agent", "metadata": {"framework": "langgraph"}},
            ]}),
        ]
        result = discover_target_metadata(model())
        self.assertEqual(result["source"], "models.list")
        self.assertEqual(result["metadata"], {"framework": "langgraph"})
        self.assertEqual(get.call_count, 2)

    @mock.patch("model_registry.remote_metadata.httpx.get")
    def test_openwebui_info_meta_is_supported(self, get):
        get.return_value = response({"id": "team/my-agent", "info": {"meta": {"description": "Agent"}}})
        self.assertEqual(discover_target_metadata(model())["metadata"], {"description": "Agent"})

    @mock.patch("model_registry.remote_metadata.httpx.get")
    def test_missing_metadata_is_optional(self, get):
        get.return_value = response({"data": [{"id": "team/my-agent", "object": "model"}]})
        result = discover_target_metadata(model())
        self.assertEqual(result["status"], "absent")
        self.assertIsNone(result["metadata"])
        self.assertEqual(get.call_count, 2)

    @mock.patch("model_registry.remote_metadata.httpx.get")
    def test_network_error_does_not_block_audit_or_retry(self, get):
        get.side_effect = httpx.ConnectError("unavailable")
        result = discover_target_metadata(model())
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["metadata"])
        self.assertEqual(get.call_count, 1)

    @mock.patch("model_registry.remote_metadata.httpx.get")
    def test_large_metadata_is_rejected(self, get):
        get.return_value = response({"id": "team/my-agent", "metadata": {"prompt": "x" * 270000}})
        result = discover_target_metadata(model())
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["metadata"])

    @mock.patch("model_registry.remote_metadata.httpx.get")
    def test_redacts_secrets_recursively(self, get):
        get.return_value = response({"id": "team/my-agent", "metadata": {
            "api_key": "secret-value", "prompts": [{"version": "1", "access_token": "private"}],
        }})
        metadata = discover_target_metadata(model())["metadata"]
        self.assertEqual(metadata["api_key"], "[REDACTED]")
        self.assertEqual(metadata["prompts"][0]["access_token"], "[REDACTED]")

    def test_flattening_is_deterministic_and_retains_leaf_types(self):
        flat = flattened_metadata({
            "prompts": {"system": {"content": "Hello", "version": "1"}},
            "retrieval": {"top_k": 0, "enabled": False, "filter": None},
            "tools": ["search", "calculator"], "empty": {},
        })
        self.assertEqual(flat["prompts.system.content"], "Hello")
        self.assertEqual(flat["retrieval.enabled"], "false")
        self.assertEqual(flat["retrieval.top_k"], "0")
        self.assertEqual(flat["retrieval.filter"], "null")
        self.assertEqual(flat["tools"], '["search", "calculator"]')
        self.assertEqual(flat["empty"], "{}")


def owui_model():
    return SimpleNamespace(
        model_id="studio.agent-1",
        connection=SimpleNamespace(
            provider="openai", base_url="http://127.0.0.1:8080/chat/api/v1",
            secret_reference="", api_key_direct=""
        ),
    )


class OpenWebUITrustedHeaderDiscoveryTests(SimpleTestCase):
    """Connections pointing at Studio's own Open WebUI sign in via the
    trusted-header flow instead of requiring a stored API key."""

    def test_trusted_header_token_authenticates_probes(self):
        with mock.patch("model_registry.remote_metadata.httpx.get") as get, \
                mock.patch("model_registry.remote_metadata.chat_config.UPSTREAM", "http://127.0.0.1:8080/chat"), \
                mock.patch("model_registry.remote_metadata.ChatAPI.sign_in") as sign_in:
            sign_in.return_value = {"token": "owui-session-token"}
            get.return_value = response({"id": "studio.agent-1", "info": {"meta": {"description": "Agent"}}})
            result = discover_target_metadata(owui_model(), user=self._make_user())
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["source"], "models.retrieve")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], ("Bea" + "rer ") + "owui-session-token")
        sign_in.assert_called_once()

    def test_falls_back_to_list_when_detail_has_no_meta(self):
        with mock.patch("model_registry.remote_metadata.httpx.get") as get, \
                mock.patch("model_registry.remote_metadata.chat_config.UPSTREAM", "http://127.0.0.1:8080/chat"), \
                mock.patch("model_registry.remote_metadata.ChatAPI.sign_in") as sign_in:
            sign_in.return_value = {"token": "t"}
            get.side_effect = [
                response({"id": "studio.agent-1", "object": "model"}),
                response({"data": [{"id": "studio.agent-1", "info": {"meta": {"tags": ["a"]}}}]}),
            ]
            result = discover_target_metadata(owui_model(), user=self._make_user())
        self.assertEqual(result["source"], "models.list")
        self.assertEqual(result["metadata"], {"tags": ["a"]})
        self.assertEqual(get.call_count, 2)

    def test_foreign_url_never_uses_trusted_headers(self):
        # A third-party endpoint must only ever see the stored Bearer key.
        m = model()  # https://example.invalid/v1 with api_key_direct="test-key"
        with mock.patch("model_registry.remote_metadata.httpx.get") as get:
            get.return_value = response({"id": "team/my-agent", "metadata": {"ok": True}})
            discover_target_metadata(m, user=self._make_user())
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], ("Bea" + "rer ") + "test-key")

    def test_sign_in_failure_marks_unavailable_without_blocking(self):
        from chat.api import ChatAPIError

        with mock.patch("model_registry.remote_metadata.httpx.get") as get, \
                mock.patch("model_registry.remote_metadata.chat_config.UPSTREAM", "http://127.0.0.1:8080/chat"), \
                mock.patch("model_registry.remote_metadata.ChatAPI.sign_in") as sign_in:
            sign_in.side_effect = ChatAPIError("refused")
            result = discover_target_metadata(owui_model(), user=self._make_user())
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["metadata"])
        get.assert_not_called()

    def _make_user(self):
        from types import SimpleNamespace as NS

        user = NS(email="admin@example.local", username="studio", is_superuser=True)
        user.get_full_name = lambda: "Studio Admin"
        return user
