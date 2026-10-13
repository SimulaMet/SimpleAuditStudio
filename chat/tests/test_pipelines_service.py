"""The self-contained Pipelines filter service (chat -> Langfuse tracing).

Run:
    uv run pytest chat/tests/test_pipelines_service.py -q
"""
from django.test import SimpleTestCase
from fastapi.testclient import TestClient

from chat import pipelines_service as svc


class _Base(SimpleTestCase):
    def setUp(self):
        # No Langfuse keys in tests: tracing is disabled, capture still runs.
        self._secret = svc.LANGFUSE_SECRET_KEY
        self._public = svc.LANGFUSE_PUBLIC_KEY
        self._canned = svc.CANNED
        self._forward = svc.FORWARD_TO
        svc.LANGFUSE_SECRET_KEY = ""
        svc.LANGFUSE_PUBLIC_KEY = ""
        svc._langfuse = None
        svc._chat_trace_ids.clear()
        self.client = TestClient(svc.app)
        self.auth = {"Authorization": f"Bearer {svc.API_KEY}"}

    def tearDown(self):
        svc.LANGFUSE_SECRET_KEY = self._secret
        svc.LANGFUSE_PUBLIC_KEY = self._public
        svc.CANNED = self._canned
        svc.FORWARD_TO = self._forward
        svc._langfuse = None


class ModelListingTests(_Base):
    def test_lists_the_filter_as_a_model(self):
        res = self.client.get("/v1/models", headers=self.auth)
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(data[0]["id"], svc.PIPELINE_ID)
        self.assertEqual(data[0]["pipeline"]["type"], "filter")

    def test_lists_registered_pipelines(self):
        res = self.client.get("/v1/pipelines", headers=self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"][0]["id"], svc.PIPELINE_ID)

    def test_rejects_a_wrong_key(self):
        res = self.client.get(
            "/v1/models", headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(res.status_code, 401)


class CapturePassthroughTests(_Base):
    def test_inlet_returns_body_unchanged(self):
        body = {
            "model": svc.PIPELINE_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"chat_id": "c-1"},
        }
        res = self.client.post(
            f"/v1/{svc.PIPELINE_ID}/filter/inlet",
            json={"body": body, "user": {"email": "a@b.c"}},
            headers=self.auth,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), body)

    def test_outlet_returns_body_unchanged(self):
        body = {
            "model": svc.PIPELINE_ID,
            "metadata": {"chat_id": "c-1"},
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello",
                 "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
            ],
        }
        res = self.client.post(
            f"/v1/{svc.PIPELINE_ID}/filter/outlet",
            json={"body": body, "user": {"email": "a@b.c"}},
            headers=self.auth,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), body)

    def test_unknown_pipeline_is_404(self):
        res = self.client.post(
            "/v1/other/filter/inlet",
            json={"body": {"model": "x", "messages": []}},
            headers=self.auth,
        )
        self.assertEqual(res.status_code, 404)


class CannedCompletionTests(_Base):
    def test_canned_mode_answers_without_upstream(self):
        svc.CANNED = True
        svc.FORWARD_TO = ""
        res = self.client.post(
            "/v1/chat/completions",
            json={"model": svc.PIPELINE_ID,
                  "messages": [{"role": "user", "content": "ping"}]},
            headers=self.auth,
        )
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual(payload["object"], "chat.completion")
        content = payload["choices"][0]["message"]["content"]
        self.assertIn("echo: ping", content)
