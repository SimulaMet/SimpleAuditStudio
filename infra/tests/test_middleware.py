"""Tests for request-ID middleware and correlation logging."""
import logging

from django.test import TestCase

from infra.middleware import CorrelationLogFilter


class RequestIDMiddlewareTest(TestCase):
    def test_response_has_request_id_header(self):
        resp = self.client.get("/healthz")
        self.assertIn("X-Request-ID", resp.headers)
        self.assertTrue(len(resp.headers["X-Request-ID"]) >= 8)

    def test_upstream_request_id_honoured(self):
        resp = self.client.get("/healthz", HTTP_X_REQUEST_ID="my-custom-id-123")
        self.assertEqual(resp.headers["X-Request-ID"], "my-custom-id-123")

    def test_different_requests_get_different_ids(self):
        r1 = self.client.get("/healthz")
        r2 = self.client.get("/healthz")
        self.assertNotEqual(r1.headers["X-Request-ID"], r2.headers["X-Request-ID"])


class CorrelationLogFilterTest(TestCase):
    def test_filter_attaches_fields_to_record(self):
        CorrelationLogFilter.set_context(request_id="abc123", user_id=42)
        try:
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname=__file__,
                lineno=1, msg="hello", args=(), exc_info=None,
            )
            f = CorrelationLogFilter()
            f.filter(record)
            self.assertEqual(getattr(record, "request_id", None), "abc123")
            self.assertEqual(getattr(record, "user_id", None), 42)
        finally:
            CorrelationLogFilter.clear_context()

    def test_clear_removes_fields(self):
        CorrelationLogFilter.set_context(request_id="xyz")
        CorrelationLogFilter.clear_context()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__,
            lineno=1, msg="hello", args=(), exc_info=None,
        )
        f = CorrelationLogFilter()
        f.filter(record)
        self.assertIsNone(getattr(record, "request_id", None))
