"""Talking to Open WebUI: the merge rules, and a round trip against a stub.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test chat.tests.test_api
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import SimpleTestCase, TestCase

from accounts.models import ProjectMembership
from chat.api import STUDIO_MARKER, ChatAPI, ChatAPIError, plan_openai_config
from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory


class PlanOpenAIConfigTests(SimpleTestCase):
    """The merge that keeps hand-added providers and replaces Studio's own."""

    def test_pushes_connections_into_an_empty_config(self):
        planned = plan_openai_config({}, [
            {"id": 7, "name": "OpenAI", "base_url": "https://api.openai.com/v1",
             "api_key": "sk-x", "enabled": True},
        ])
        self.assertEqual(planned["OPENAI_API_BASE_URLS"], ["https://api.openai.com/v1"])
        self.assertEqual(planned["OPENAI_API_KEYS"], ["sk-x"])
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["0"][STUDIO_MARKER], 7)
        self.assertIs(planned["ENABLE_OPENAI_API"], True)

    def test_keeps_providers_added_in_open_webui(self):
        current = {
            "OPENAI_API_BASE_URLS": ["https://theirs.example/v1"],
            "OPENAI_API_KEYS": ["theirs"],
            "OPENAI_API_CONFIGS": {"0": {"enable": True}},
        }
        planned = plan_openai_config(current, [
            {"id": 1, "name": "Ours", "base_url": "https://ours.example/v1",
             "api_key": "ours", "enabled": True},
        ])
        self.assertEqual(
            planned["OPENAI_API_BASE_URLS"],
            ["https://theirs.example/v1", "https://ours.example/v1"],
        )
        self.assertNotIn(STUDIO_MARKER, planned["OPENAI_API_CONFIGS"]["0"])
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["1"][STUDIO_MARKER], 1)

    def test_replaces_what_studio_pushed_before(self):
        current = plan_openai_config({}, [
            {"id": 1, "name": "Old", "base_url": "https://old.example/v1",
             "api_key": "old", "enabled": True},
        ])
        planned = plan_openai_config(current, [
            {"id": 2, "name": "New", "base_url": "https://new.example/v1",
             "api_key": "new", "enabled": True},
        ])
        self.assertEqual(planned["OPENAI_API_BASE_URLS"], ["https://new.example/v1"])

    def test_dropping_every_connection_leaves_only_foreign_entries(self):
        current = {
            "OPENAI_API_BASE_URLS": ["https://theirs.example/v1", "https://ours.example/v1"],
            "OPENAI_API_KEYS": ["theirs", "ours"],
            "OPENAI_API_CONFIGS": {"0": {}, "1": {STUDIO_MARKER: 4}},
        }
        planned = plan_openai_config(current, [])
        self.assertEqual(planned["OPENAI_API_BASE_URLS"], ["https://theirs.example/v1"])
        self.assertEqual(planned["OPENAI_API_KEYS"], ["theirs"])

    def test_shorter_key_list_does_not_lose_urls(self):
        current = {
            "OPENAI_API_BASE_URLS": ["https://a.example/v1", "https://b.example/v1"],
            "OPENAI_API_KEYS": ["only-one"],
            "OPENAI_API_CONFIGS": {},
        }
        planned = plan_openai_config(current, [])
        self.assertEqual(len(planned["OPENAI_API_BASE_URLS"]), 2)
        self.assertEqual(planned["OPENAI_API_KEYS"], ["only-one", ""])

    def _assert_aligned(self, planned):
        """The three structures are the same length and keyed by the same indices."""
        urls = planned["OPENAI_API_BASE_URLS"]
        keys = planned["OPENAI_API_KEYS"]
        configs = planned["OPENAI_API_CONFIGS"]
        self.assertEqual(len(urls), len(keys))
        self.assertEqual(len(urls), len(configs))
        self.assertEqual(set(configs), {str(index) for index in range(len(urls))})
        for index, url in enumerate(urls):
            self.assertIsInstance(configs[str(index)], dict)
            self.assertIsInstance(keys[index], str)
            self.assertIn(url, urls)

    def test_orphan_key_beyond_the_url_list_is_dropped_not_mispaired(self):
        current = {
            "OPENAI_API_BASE_URLS": ["https://a.example/v1"],
            "OPENAI_API_KEYS": ["key-a", "orphan-key"],
            "OPENAI_API_CONFIGS": {"0": {"enable": True}},
        }
        planned = plan_openai_config(current, [
            {"id": 1, "name": "Ours", "base_url": "https://ours.example/v1",
             "api_key": "ours", "enabled": True},
        ])
        self.assertEqual(planned["OPENAI_API_BASE_URLS"],
                         ["https://a.example/v1", "https://ours.example/v1"])
        # The orphan key must not ride along on the second URL.
        self.assertEqual(planned["OPENAI_API_KEYS"], ["key-a", "ours"])
        self.assertNotIn("orphan-key", planned["OPENAI_API_KEYS"])
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["0"], {"enable": True})
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["1"][STUDIO_MARKER], 1)
        self._assert_aligned(planned)

    def test_config_index_without_a_url_is_dropped(self):
        current = {
            "OPENAI_API_BASE_URLS": ["https://a.example/v1"],
            "OPENAI_API_KEYS": ["key-a"],
            "OPENAI_API_CONFIGS": {"0": {"enable": True}, "5": {"enable": True}},
        }
        planned = plan_openai_config(current, [
            {"id": 2, "name": "Ours", "base_url": "https://ours.example/v1",
             "api_key": "ours", "enabled": True},
        ])
        self.assertEqual(planned["OPENAI_API_BASE_URLS"],
                         ["https://a.example/v1", "https://ours.example/v1"])
        self.assertEqual(planned["OPENAI_API_KEYS"], ["key-a", "ours"])
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["0"], {"enable": True})
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["1"][STUDIO_MARKER], 2)
        self._assert_aligned(planned)

    def test_url_without_a_key_gets_an_empty_key_at_its_own_index(self):
        current = {
            "OPENAI_API_BASE_URLS": ["https://a.example/v1", "https://b.example/v1"],
            "OPENAI_API_KEYS": ["key-a"],
            "OPENAI_API_CONFIGS": {},
        }
        planned = plan_openai_config(current, [
            {"id": 3, "name": "Ours", "base_url": "https://ours.example/v1",
             "api_key": "ours", "enabled": True},
        ])
        self.assertEqual(planned["OPENAI_API_BASE_URLS"],
                         ["https://a.example/v1", "https://b.example/v1", "https://ours.example/v1"])
        # key-a stays with a.example; b.example gets its own empty key, not key-a.
        self.assertEqual(planned["OPENAI_API_KEYS"], ["key-a", "", "ours"])
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["0"], {})
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["1"], {})
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["2"][STUDIO_MARKER], 3)
        self._assert_aligned(planned)

    def test_aligned_input_with_mixed_entries_merges_as_before(self):
        current = {
            "OPENAI_API_BASE_URLS": [
                "https://theirs1.example/v1", "https://theirs2.example/v1", "https://old.example/v1",
            ],
            "OPENAI_API_KEYS": ["k1", "k2", "old"],
            "OPENAI_API_CONFIGS": {
                "0": {"enable": True},
                "1": {"name": "Theirs 2"},
                "2": {STUDIO_MARKER: 9},
            },
        }
        planned = plan_openai_config(current, [
            {"id": 3, "name": "Ours", "base_url": "https://ours.example/v1",
             "api_key": "ours", "enabled": True},
        ])
        self.assertEqual(planned["OPENAI_API_BASE_URLS"],
                         ["https://theirs1.example/v1", "https://theirs2.example/v1", "https://ours.example/v1"])
        self.assertEqual(planned["OPENAI_API_KEYS"], ["k1", "k2", "ours"])
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["0"], {"enable": True})
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["1"], {"name": "Theirs 2"})
        self.assertEqual(planned["OPENAI_API_CONFIGS"]["2"][STUDIO_MARKER], 3)
        self._assert_aligned(planned)


class _StubOpenWebUI(BaseHTTPRequestHandler):
    """Just enough Open WebUI to answer sign-in, config and knowledge."""

    protocol_version = "HTTP/1.1"
    state: dict = {}

    def log_message(self, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/v1/auths/signin":
            email = self.headers.get("X-Studio-Email")
            if not email:
                return self._reply(400, {"detail": "no trusted header"})
            self.state["signed_in_as"] = email
            self.state["role"] = self.headers.get("X-Studio-Role")
            self.state["token"] = "t0ken"
            return self._reply(200, {"token": self.state["token"], "email": email})
        if self.headers.get("Authorization") != ("Bear" + "er " + self.state.get("token", "")):
            return self._reply(401, {"detail": "no token"})
        if self.path == "/openai/config/update":
            self.state["config"] = body
            return self._reply(200, body)
        return self._reply(404, {"detail": "nope"})

    def do_GET(self):
        if self.headers.get("Authorization") != ("Bear" + "er " + self.state.get("token", "")):
            return self._reply(401, {"detail": "no token"})
        if self.path == "/openai/config":
            return self._reply(200, self.state.get("config", {}))
        if self.path == "/api/v1/knowledge/":
            return self._reply(200, [
                {"id": "kb1", "name": "Policies", "description": "HR", "files": [{"id": "f1"}]},
            ])
        if self.path == "/api/v1/knowledge/kb1":
            return self._reply(200, {
                "id": "kb1", "name": "Policies", "description": "HR",
                "files": [{"id": "f1", "meta": {"name": "handbook.pdf"}}],
            })
        if self.path == "/api/v1/models/":
            return self._reply(200, self.state.get("models", {
                "models": [
                    {"id": "gpt-4o", "object": "model"},
                    {"id": "gpt-4o-mini", "object": "model"},
                ],
            }))
        return self._reply(404, {"detail": "nope"})


class ChatAPITests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _StubOpenWebUI.state = {}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOpenWebUI)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        _StubOpenWebUI.state = {}

    def _api(self, **user_kwargs):
        user = UserFactory(**user_kwargs)
        MembershipFactory(user=user, project=ProjectFactory(), role=ProjectMembership.Role.ADMIN)
        from chat.config import identity

        return ChatAPI(identity(user), base_url=self.url)

    def test_signs_in_with_the_trusted_headers(self):
        api = self._api(username="pusher")
        api.sign_in()
        self.assertEqual(_StubOpenWebUI.state["signed_in_as"], "pusher@test.com")
        self.assertEqual(_StubOpenWebUI.state["role"], "admin")

    def test_push_connections_reports_what_it_did(self):
        api = self._api(username="pusher2")
        result = api.push_connections([
            {"id": 1, "name": "OpenAI", "base_url": "https://api.openai.com/v1",
             "api_key": "sk-x", "enabled": True},
        ])
        self.assertEqual(result, {"pushed": 1, "kept": 0})
        self.assertEqual(
            _StubOpenWebUI.state["config"]["OPENAI_API_BASE_URLS"],
            ["https://api.openai.com/v1"],
        )

    def test_knowledge_bases_are_normalised(self):
        bases = self._api(username="reader").knowledge_bases()
        self.assertEqual(bases, [{
            "id": "kb1", "name": "Policies", "description": "HR",
            "file_count": 1, "updated_at": None,
        }])

    def test_knowledge_base_lists_file_names(self):
        base = self._api(username="reader2").knowledge_base("kb1")
        self.assertEqual(base["files"], [{"id": "f1", "name": "handbook.pdf"}])

    def test_list_models_returns_the_registered_ids(self):
        self.assertEqual(
            self._api(username="reader3").list_models(),
            ["gpt-4o", "gpt-4o-mini"],
        )

    def test_list_models_copes_with_a_bare_list_payload(self):
        _StubOpenWebUI.state["models"] = [{"id": "local-model"}]
        self.assertEqual(self._api(username="reader4").list_models(), ["local-model"])

    def test_an_unreachable_open_webui_is_reported_clearly(self):
        api = ChatAPI({"X-Studio-Email": "x@y.z"}, base_url="http://127.0.0.1:1")
        with self.assertRaises(ChatAPIError) as caught:
            api.knowledge_bases()
        self.assertIn("Could not reach Open WebUI", str(caught.exception))
