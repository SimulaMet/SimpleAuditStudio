"""Tests for deleting model connections from the models page.

Deleting a connection cascades to its RegisteredModels, but AuditRun pins
models via RESTRICT FKs (immutable experiment records). The view must catch
the resulting ProtectedError and show a friendly banner instead of 500ing.
"""
from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)
from model_registry.models import ModelConnection


class ConnectionDeleteTest(TestCase):
    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=pw)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()

    def test_delete_unreferenced_connection_succeeds(self):
        conn = ModelConnectionFactory(project=self.project, name="unused-conn")
        RegisteredModelFactory(connection=conn, project=self.project)

        resp = self.client.post(f"/connections/{conn.id}/delete/")

        assert resp.status_code == 302
        assert resp.url == "/connections/"
        assert not conn.__class__.objects.filter(pk=conn.id).exists()

    def test_delete_referenced_connection_is_blocked_with_message(self):
        conn = ModelConnectionFactory(project=self.project, name="pinned-conn")
        pinned_model = RegisteredModelFactory(connection=conn, project=self.project)
        AuditRunFactory(project=self.project, target_model=pinned_model)

        resp = self.client.post(f"/connections/{conn.id}/delete/")

        assert resp.status_code == 302
        assert resp.url == "/connections/"
        # Connection and its models must still exist.
        assert conn.__class__.objects.filter(pk=conn.id).exists()
        assert pinned_model.__class__.objects.filter(pk=pinned_model.id).exists()
        # The error banner is rendered on the next page load (base.html iterates
        # the messages framework), so follow the redirect and check the body.
        get_resp = self.client.get("/connections/")
        body = get_resp.content.decode()
        assert "runs or monitors use it" in body

    def test_delete_other_project_connection_is_noop(self):
        other_project = ProjectFactory()
        conn = ModelConnectionFactory(project=other_project, name="foreign-conn")

        resp = self.client.post(f"/connections/{conn.id}/delete/")

        assert resp.status_code == 302
        assert conn.__class__.objects.filter(pk=conn.id).exists()


class RegisteredModelDeleteTest(TestCase):
    """Deleting a single model (not its connection) must also be guarded."""

    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=pw)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()

    def test_delete_unreferenced_model_succeeds(self):
        from model_registry.models import RegisteredModel

        conn = ModelConnectionFactory(project=self.project, name="conn-a")
        rm = RegisteredModelFactory(connection=conn, project=self.project)

        resp = self.client.post("/connections/", {"action": "delete_model", "rm_id": rm.id}, follow=True)

        assert resp.status_code == 200
        assert not RegisteredModel.objects.filter(pk=rm.id).exists()

    def test_delete_referenced_model_is_blocked_with_error(self):
        from model_registry.models import RegisteredModel

        conn = ModelConnectionFactory(project=self.project, name="conn-b")
        rm = RegisteredModelFactory(connection=conn, project=self.project)
        AuditRunFactory(project=self.project, target_model=rm)

        resp = self.client.post("/connections/", {"action": "delete_model", "rm_id": rm.id}, follow=True)

        assert resp.status_code == 200
        # Model must still exist.
        assert RegisteredModel.objects.filter(pk=rm.id).exists()
        body = resp.content.decode()
        assert "runs or monitors use it" in body


class DiscoverModelsKeyTests(TestCase):
    """The Connections page must never render API keys; discover resolves them server-side."""

    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="viewer")
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")
        self.conn = ModelConnection.objects.create(
            project=self.project, name="Secret conn", provider="openai",
            base_url="https://api.example.invalid/v1", api_key_direct="sk-super-secret-123",
        )

    def test_models_page_does_not_contain_the_key(self):
        page = self.client.get("/connections/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "sk-super-secret-123")
        self.assertContains(page, f'data-discover="{self.conn.id}"')

    def test_discover_uses_stored_key_for_own_connection_only(self):
        from unittest import mock

        fake = mock.MagicMock()
        fake.json.return_value = {"data": [{"id": "gpt-x"}]}
        with mock.patch("model_registry.services.httpx.get", return_value=fake) as get:
            resp = self.client.post("/connections/discover/", {"connection_id": self.conn.id})
        # Non-Ollama servers: ids only, no decision probe (httpx.post unused).
        self.assertEqual(resp.json()["models"], [{"id": "gpt-x", "description": "", "capabilities": {}}])
        url, kwargs = get.call_args.args[0], get.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-super-secret-123")
        self.assertEqual(url, "https://api.example.invalid/v1/models")

        other = ModelConnection.objects.create(project=ProjectFactory(), name="Other", base_url="https://x.invalid/v1")
        self.assertEqual(self.client.post("/connections/discover/", {"connection_id": other.id}).status_code, 404)


class ConnectionCheckTests(TestCase):
    """The connection dialog's Check tries unsaved settings before saving."""

    def setUp(self):
        from unittest import mock

        self.mock = mock
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        self.membership = MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")

    def _server(self, ids=("a", "b")):
        fake = self.mock.MagicMock()
        fake.json.return_value = {"data": [{"id": i} for i in ids]}
        return self.mock.patch("model_registry.services.httpx.get", return_value=fake)

    def test_typed_settings_are_tried(self):
        with self._server(["m1", "m2"]) as get:
            out = self.client.post("/connections/check/", {
                "conn_provider": "openai", "conn_base_url": "https://new.invalid/v1",
                "key_mode": "stored", "conn_api_key": "sk-typed",
            }).json()
        self.assertEqual(out, {"ok": True, "count": 2, "models": ["m1", "m2"]})
        self.assertEqual(get.call_args.args[0], "https://new.invalid/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer sk-typed")
        self.assertFalse(ModelConnection.objects.exists())   # nothing saved

    def test_editing_with_blank_key_uses_the_stored_one(self):
        conn = ModelConnection.objects.create(project=self.project, name="C", base_url="https://c.invalid/v1",
                                              api_key_direct="sk-stored")
        with self._server() as get:
            self.client.post("/connections/check/", {"conn_id": conn.id, "conn_base_url": conn.base_url,
                                                     "key_mode": "stored", "conn_api_key": ""})
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer sk-stored")

    def test_another_workspaces_key_is_never_used(self):
        other = ModelConnection.objects.create(project=ProjectFactory(), name="O", base_url="https://o.invalid/v1",
                                               api_key_direct="sk-not-yours")
        with self._server() as get:
            self.client.post("/connections/check/", {"conn_id": other.id, "conn_base_url": other.base_url,
                                                     "key_mode": "stored"})
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_errors_are_reported(self):
        import httpx

        self.assertIn("base URL", self.client.post("/connections/check/", {"conn_base_url": ""}).json()["error"])
        with self.mock.patch("model_registry.services.httpx.get", side_effect=httpx.ConnectError("refused")):
            out = self.client.post("/connections/check/", {"conn_base_url": "https://down.invalid/v1"}).json()
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"])

    def test_a_web_page_is_explained(self):
        fake = self.mock.MagicMock()
        fake.json.side_effect = ValueError("Expecting value")
        with self.mock.patch("model_registry.services.httpx.get", return_value=fake):
            out = self.client.post("/connections/check/", {"conn_base_url": "https://site.invalid"}).json()
        self.assertIn("usually ends in /v1", out["error"])

    def test_viewers_cannot_check(self):
        self.membership.role = "viewer"
        self.membership.save()
        self.assertEqual(self.client.post("/connections/check/", {"conn_base_url": "https://x.invalid/v1"}).status_code, 403)

    def test_each_provider_listed_once(self):
        ModelConnection.objects.create(project=self.project, name="G", provider="gemini", base_url="https://g.invalid")
        providers = self.client.get("/connections/").context["providers"]
        self.assertEqual(providers.count("openai"), 1)
        self.assertIn("gemini", providers)   # a connection's own provider stays pickable


class OllamaDecisionProbeTests(TestCase):
    """Ollama answers the System One probe with a 400 "does not support decision"
    for chat models and a real answer for decision models — no inference either way."""

    def _post(self, status=200, text="", json=None):
        import httpx

        kwargs = {"request": httpx.Request("POST", "http://x/v1/systemone")}
        if json is not None:
            kwargs["json"] = json
        else:
            kwargs["text"] = text
        return httpx.Response(status, **kwargs)

    def _probing(self, status=200, text="", json=None):
        from unittest import mock

        return mock.patch(
            "model_registry.services.httpx.post", return_value=self._post(status, text, json)
        )

    def test_decision_model_returns_true(self):
        from model_registry.services import probe_ollama_decision

        with self._probing(json={"answers": {"probe": {"choice": "yes"}}}):
            self.assertIs(probe_ollama_decision("http://localhost:11434/v1", "clef:1.0"), True)

    def test_chat_model_returns_false(self):
        from model_registry.services import probe_ollama_decision

        with self._probing(400, text='{"error":"model \"qwen3.5:2b\" does not support decision"}'):
            self.assertIs(probe_ollama_decision("http://localhost:11434/v1", "qwen3.5:2b"), False)

    def test_unknown_error_is_none(self):
        from model_registry.services import probe_ollama_decision

        with self._probing(404, text='{"error":"model \"x\" not found, try pulling it first"}'):
            self.assertIsNone(probe_ollama_decision("http://localhost:11434/v1", "x"))

    def test_network_error_is_none(self):
        from unittest import mock

        import httpx

        from model_registry.services import probe_ollama_decision

        with mock.patch("model_registry.services.httpx.post", side_effect=httpx.ConnectError("refused")):
            self.assertIsNone(probe_ollama_decision("http://localhost:11434/v1", "clef"))

    def test_probe_uses_the_api_root_not_the_openai_prefix(self):

        from model_registry.services import probe_ollama_decision

        with self._probing(json={}) as post:
            probe_ollama_decision("http://localhost:11434/v1", "clef")
        self.assertEqual(post.call_args.args[0], "http://localhost:11434/v1/systemone")


class DetectModelCapabilitiesTests(TestCase):
    """Detection of decision (System One) models per provider."""

    def _conn(self, **kw):
        from model_registry.models import ModelConnection

        defaults = {
            "project": self.project, "name": "det", "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
        }
        defaults.update(kw)
        return ModelConnection(**defaults)

    def setUp(self):
        from infra.tests.factories import ProjectFactory

        self.project = ProjectFactory()

    def test_openrouter_by_name_without_network(self):
        from model_registry.services import detect_model_capabilities

        conn = self._conn(provider="openrouter", base_url="https://openrouter.ai/api/v1")
        self.assertEqual(detect_model_capabilities(conn, "typesafe/jev-1.13"), {"decision": True})
        self.assertEqual(detect_model_capabilities(conn, "openai/gpt-4o"), {})

    def test_ollama_probe_decision_true(self):
        from unittest import mock

        from model_registry.services import detect_model_capabilities

        conn = self._conn()
        with mock.patch("model_registry.services.httpx.post") as post:
            post.return_value = mock.MagicMock(status_code=200, text="")
            self.assertEqual(detect_model_capabilities(conn, "clef:1.0"), {"decision": True})

    def test_ollama_probe_decision_false(self):
        from unittest import mock

        from model_registry.services import detect_model_capabilities

        conn = self._conn()
        with mock.patch("model_registry.services.httpx.post") as post:
            post.return_value = mock.MagicMock(status_code=400, text='{"error":"does not support decision"}')
            # A definitive "not a decision model" answer is recorded, not dropped.
            self.assertEqual(detect_model_capabilities(conn, "qwen3.5:2b"), {"decision": False})

    def test_ollama_unknown_probe_falls_back_to_library_name(self):
        from unittest import mock

        from model_registry.services import detect_model_capabilities

        conn = self._conn()
        with mock.patch("model_registry.services.httpx.post") as post:
            post.return_value = mock.MagicMock(status_code=400, text='{"error":"model not found"}')
            self.assertEqual(detect_model_capabilities(conn, "nimble:latest"), {"decision": True})
            self.assertEqual(detect_model_capabilities(conn, "llama3.2:3b"), {})


class DecisionModelConnectionTests(TestCase):
    """/connections/ discover marks decision models; add_models persists the flag."""

    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        auth = {"username": self.user.username, "password": pw}
        self.client.login(**auth)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()
        self.conn = ModelConnection.objects.create(
            project=self.project, name="ollama-local", provider="ollama",
            base_url="http://localhost:11434/v1",
        )

    def _server(self, model_ids):
        from unittest import mock

        get = mock.MagicMock()
        get.json.return_value = {"models": [{"name": i, "model": i} for i in model_ids]}
        post = mock.MagicMock()
        return mock.patch("model_registry.services.httpx.get", return_value=get), mock.patch(
            "model_registry.services.httpx.post", return_value=post
        )

    def test_discover_flags_decision_models(self):
        from unittest import mock

        get_ctx, post_ctx = self._server(["clef:1.0", "qwen3.5:2b"])
        with get_ctx, post_ctx as post:
            # First probe: decision model (200). Second: chat model (400).
            r1 = mock.MagicMock(status_code=200, text="")
            r2 = mock.MagicMock(status_code=400, text='{"error":"does not support decision"}')
            post.side_effect = [r1, r2]
            models = self.client.post("/connections/discover/", {"connection_id": self.conn.id}).json()["models"]
        by_id = {m["id"]: m for m in models}
        self.assertEqual(by_id["clef:1.0"]["capabilities"], {"decision": True})
        self.assertEqual(by_id["qwen3.5:2b"]["capabilities"], {"decision": False})
        # The probe body carries a two-option question (the endpoint minimum).
        first_body = post.call_args_list[0].kwargs["json"]
        self.assertEqual(first_body["model"], "clef:1.0")
        self.assertEqual(len(first_body["questions"]["probe"]["criteria"]), 2)

    def test_add_models_persists_the_flag(self):
        from model_registry.models import RegisteredModel

        get_ctx, post_ctx = self._server([])
        with get_ctx, post_ctx as post:
            resp = self.client.post("/connections/", {
                "action": "add_models", "conn_id": self.conn.id,
                "model_id": ["clef:1.0", "qwen3.5:2b"],
                "decision:clef:1.0": "1",
            }, follow=True)
        self.assertEqual(resp.status_code, 200)
        # The flagged model was trusted as-is; only the unflagged one was probed.
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "qwen3.5:2b")
        self.assertEqual(
            RegisteredModel.objects.get(model_id="clef:1.0").capabilities, {"decision": True}
        )

    def test_add_models_probes_a_manual_id(self):
        from unittest import mock

        from model_registry.models import RegisteredModel

        get_ctx, post_ctx = self._server([])
        with get_ctx, post_ctx as post:
            post.return_value = mock.MagicMock(status_code=400, text='{"error":"does not support decision"}')
            self.client.post("/connections/", {
                "action": "add_models", "conn_id": self.conn.id,
                "model_id": ["qwen3.5:2b"],
            }, follow=True)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            RegisteredModel.objects.get(model_id="qwen3.5:2b").capabilities, {"decision": False}
        )

    def test_page_shows_the_decision_badge(self):
        from model_registry.models import RegisteredModel

        RegisteredModel.objects.create(
            connection=self.conn, project=self.project, model_id="clef:1.0",
            display_name="clef:1.0", capabilities={"decision": True},
        )
        body = self.client.get("/connections/").content.decode()
        self.assertIn(">decision</span>", body)
