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

    def test_auditable_spans_keep_genai_kind(self):
        otlp_views.clear_target_spans(self.target_id)
        body = _export([
            _span("05" * 8, "chat gpt", attributes={"gen_ai.operation.name": "chat"}),
        ])
        self._post(body, headers={"HTTP_AUTHORIZATION": _basic_header(
            self.basic.credential.username, self.basic.secret)})
        rows = otlp_views.get_spans_for_trace_db(self.target_id, TRACE_ID)
        self.assertEqual(rows[0]["kind"], "LLM")
        self.assertNotIn("openinference.span.kind", rows[0]["attributes"])

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

    def test_error_span_is_stored_as_error_with_exception_message(self):
        otlp_views.clear_target_spans(self.target_id)
        span = _span("09" * 8, "execute_tool", attributes={"gen_ai.tool.name": "acme_get_order"})
        span["status"] = {"code": "STATUS_CODE_ERROR", "message": "ValueError"}
        span["events"] = [{
            "name": "exception",
            "attributes": [
                {"key": "exception.type", "value": {"stringValue": "ValueError"}},
                {"key": "exception.message", "value": {"stringValue": "Unknown order ACME-9999"}},
            ],
        }]
        resp = self._post(_export([span]), headers={"HTTP_AUTHORIZATION": _basic_header(
            self.basic.credential.username, self.basic.secret)})
        self.assertEqual(resp.status_code, 200)
        row = otlp_views.get_spans_for_trace_db(self.target_id, TRACE_ID)[0]
        self.assertEqual(row["status"], "ERROR")
        self.assertEqual(row["attributes"]["exception.message"], "Unknown order ACME-9999")


class OTLPCheapRejectTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.conn = ModelConnectionFactory(project=self.project, name="cheap-reject")
        otlp.create_credential(project=self.project, connection=self.conn, auth_mode="none")
        self.client = Client(SERVER_NAME="localhost")

    def _post(self, body, **extra):
        return self.client.post("/otlp/v1/traces", data=body, content_type="application/json", **extra)

    def test_unauthenticated_rejected_when_disallowed(self):
        from unittest import mock

        body = _export([_span("01" * 8, "chat", attributes={"gen_ai.operation.name": "chat"})])
        self.assertEqual(self._post(body).status_code, 200)
        with mock.patch("model_registry.otlp_config.ALLOW_UNAUTHENTICATED", False):
            self.assertEqual(self._post(body).status_code, 401)

    def test_oversized_body_rejected_before_parsing(self):
        from unittest import mock

        with mock.patch("model_registry.otlp_config.MAX_BODY_BYTES", 10):
            self.assertEqual(self._post(_export([_span("01" * 8, "chat")])).status_code, 413)

    def test_gzip_bomb_rejected(self):
        from unittest import mock

        bomb = gzip.compress(b"0" * 5000)
        with mock.patch("model_registry.otlp_config.MAX_BODY_BYTES", 1000):
            resp = self._post(bomb, HTTP_CONTENT_ENCODING="gzip")
        self.assertEqual(resp.status_code, 413)


class OTLPUnclaimedTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.conn = ModelConnectionFactory(project=self.project, name="unclaimed")
        self.nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer")
        self.target = self.nc.credential.target_id
        self.client = Client(SERVER_NAME="localhost")

    def _push(self, trace_id=TRACE_ID):
        span = _span("01" * 8, "chat", attributes={"gen_ai.operation.name": "chat"})
        span["traceId"] = trace_id
        return self.client.post(
            "/otlp/v1/traces", data=_export([span]), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.nc.secret}",
        )

    def test_zero_hold_stores_only_claimed_traces(self):
        from datetime import timedelta
        from unittest import mock

        from model_registry.models import OtlpSpan

        with mock.patch.object(otlp, "SPAN_HOLD_TTL", timedelta(0)):
            self._push()
            self.assertEqual(OtlpSpan.objects.filter(target_id=self.target).count(), 0)
            otlp.claim_traces(self.target, {TRACE_ID})
            self._push()
        self.assertEqual(OtlpSpan.objects.filter(target_id=self.target, held_until=None).count(), 1)

    def test_all_zero_trace_id_is_dropped(self):
        from model_registry.models import OtlpSpan

        self._push("0" * 32)
        self.assertEqual(OtlpSpan.objects.filter(target_id=self.target).count(), 0)
