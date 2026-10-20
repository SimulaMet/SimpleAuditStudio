"""OTLP ingestion keeps only audit-relevant spans and never falls back on bad auth."""
import gzip
import json

from django.test import Client, TestCase

from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    UserFactory,
)
from model_registry import otlp_services as otlp
from model_registry import otlp_views
from model_registry.tests.test_otlp_receiver import _basic_header

TRACE_ID = "ab" * 16


def _span(span_id, name, *, kind=None, attributes=None):
    span = {"traceId": TRACE_ID, "spanId": span_id, "name": name}
    if kind is not None:
        span["kind"] = kind
    if attributes:
        span["attributes"] = [
            {"key": key, "value": {"stringValue": str(value)}} for key, value in attributes.items()
        ]
    return span


def _export(spans):
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})


class OTLPIngestFilterTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="owui-filter")
        self.client = Client(SERVER_NAME="localhost")
        self.basic = otlp.create_credential(
            project=self.project, connection=self.conn, auth_mode="basic", user=self.user,
        )
        self.target_id = self.basic.credential.target_id

    def _post(self, body, *, headers=None, content_type="application/json"):
        return self.client.post(
            "/otlp/v1/traces", data=body, content_type=content_type, **(headers or {}),
        )

    def _stored_names(self):
        return sorted(r["name"] for r in otlp_views.get_spans_for_trace_db(self.target_id, TRACE_ID))

    def test_transport_and_framework_spans_are_not_stored(self):
        otlp_views.clear_target_spans(self.target_id)
        body = _export([
            _span("01" * 8, "connect", kind=3),
            _span("02" * 8, "POST https://llm.example/v1/chat/completions", kind=3),
            _span("03" * 8, "http send", kind=1),
            _span("04" * 8, "lookup_order", attributes={"gen_ai.tool.name": "acme_get_order"}),
        ])
        resp = self._post(body, headers={"HTTP_AUTHORIZATION": _basic_header(
            self.basic.credential.username, self.basic.secret)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._stored_names(), ["lookup_order"])

    def test_auditable_spans_are_tagged_with_openinference_kind(self):
        otlp_views.clear_target_spans(self.target_id)
        body = _export([
            _span("05" * 8, "chat gpt", attributes={"gen_ai.operation.name": "chat"}),
        ])
        self._post(body, headers={"HTTP_AUTHORIZATION": _basic_header(
            self.basic.credential.username, self.basic.secret)})
        rows = otlp_views.get_spans_for_trace_db(self.target_id, TRACE_ID)
        self.assertEqual(rows[0]["kind"], "LLM")

    def test_gzip_body_is_decoded(self):
        otlp_views.clear_target_spans(self.target_id)
        body = gzip.compress(_export([
            _span("06" * 8, "get", attributes={"gen_ai.tool.name": "acme_get_order"}),
        ]).encode())
        resp = self.client.post(
            "/otlp/v1/traces", data=body, content_type="application/json",
            HTTP_CONTENT_ENCODING="gzip",
            HTTP_AUTHORIZATION=_basic_header(self.basic.credential.username, self.basic.secret),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._stored_names(), ["get"])

    def test_wrong_credential_is_rejected_even_with_open_credential_present(self):
        # An enabled "none" credential must not absorb requests that present bad auth.
        otlp.create_credential(
            project=self.project, connection=self.conn, auth_mode="none", user=self.user,
        )
        body = _export([_span("07" * 8, "get", attributes={"gen_ai.tool.name": "x"})])
        resp = self._post(body, headers={"HTTP_AUTHORIZATION": _basic_header(
            self.basic.credential.username, "wrong")})
        self.assertEqual(resp.status_code, 401)

    def test_no_header_uses_open_credential(self):
        otlp.create_credential(
            project=self.project, connection=self.conn, auth_mode="none", user=self.user,
        )
        body = _export([_span("08" * 8, "get", attributes={"gen_ai.tool.name": "x"})])
        self.assertEqual(self._post(body).status_code, 200)
