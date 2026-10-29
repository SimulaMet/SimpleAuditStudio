"""Ingestion-time span selection: only audit-relevant spans survive."""
from django.test import SimpleTestCase

from audits.agentic.auditable import classify_span, select_auditable_spans
from audits.agentic.privacy import redact_sensitive


def _span(name, *, kind=None, **attrs):
    return {"name": name, "kind": kind, "attributes": attrs}


class ClassifySpanTest(SimpleTestCase):
    def test_tool_name_attribute_is_tool(self):
        self.assertEqual(classify_span(_span("x", **{"gen_ai.tool.name": "acme_get_order"})), "tool")

    def test_retrieval_attribute_is_retrieval(self):
        self.assertEqual(classify_span(_span("x", **{"openwebui.retrieval.data_source": ["kb"]})), "retrieval")

    def test_gen_ai_chat_is_inference(self):
        self.assertEqual(classify_span(_span("chat", **{"gen_ai.operation.name": "chat"})), "inference")

    def test_outbound_http_client_span_is_not_inference(self):
        span = _span("POST https://llm.example/v1/chat/completions", kind=3)
        self.assertEqual(classify_span(span), "unknown")

    def test_transport_span_named_like_a_model_call_is_unknown(self):
        span = _span("POST /api/v1/chat/completions http send", kind=1)
        self.assertEqual(classify_span(span), "unknown")

    def test_plain_connect_span_is_unknown(self):
        self.assertEqual(classify_span(_span("connect", kind=3)), "unknown")


class SelectAuditableSpansTest(SimpleTestCase):
    def test_drops_non_audit_and_tags_kept_spans(self):
        spans = [
            _span("connect", kind=3),
            _span("lookup", **{"gen_ai.tool.name": "acme_get_order"}),
            _span("chat", **{"gen_ai.operation.name": "chat"}),
        ]
        kept = select_auditable_spans(spans)
        self.assertEqual([s["name"] for s in kept], ["lookup", "chat"])
        self.assertEqual(kept[0]["attributes"]["openinference.span.kind"], "TOOL")
        self.assertEqual(kept[0]["kind"], "TOOL")
        self.assertEqual(kept[1]["attributes"]["openinference.span.kind"], "LLM")

    def test_existing_openinference_kind_is_kept(self):
        kept = select_auditable_spans([
            _span("x", **{"gen_ai.tool.name": "t", "openinference.span.kind": "GUARDRAIL"}),
        ])
        self.assertEqual(kept[0]["attributes"]["openinference.span.kind"], "GUARDRAIL")

    def test_does_not_mutate_input(self):
        original = _span("lookup", **{"gen_ai.tool.name": "t"})
        select_auditable_spans([original])
        self.assertNotIn("openinference.span.kind", original["attributes"])


class RedactionTest(SimpleTestCase):
    def test_token_usage_counts_are_not_redacted(self):
        attrs = redact_sensitive({"attributes": {
            "gen_ai.usage.input_tokens": 120,
            "gen_ai.usage.output_tokens": 40,
            "gen_ai.request.max_tokens": 512,
        }})["attributes"]
        self.assertEqual(attrs["gen_ai.usage.input_tokens"], 120)
        self.assertEqual(attrs["gen_ai.usage.output_tokens"], 40)
        self.assertEqual(attrs["gen_ai.request.max_tokens"], 512)

    def test_access_token_is_still_redacted(self):
        out = redact_sensitive({"attributes": {"access_token": "abc"}})["attributes"]
        self.assertEqual(out["access_token"], "[REDACTED]")

    def test_plain_prose_after_basic_is_not_redacted(self):
        out = redact_sensitive({"name": "a basic refund question"})
        self.assertEqual(out["name"], "a basic refund question")
