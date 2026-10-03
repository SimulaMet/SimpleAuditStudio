"""The result page's Target trace card: _rep_view surfaces the rep's trace ids
and the captured evidence spans.

The card must stay readable for both flavors of target instrumentation:
OpenInference-style spans (semantic kinds: LLM, RETRIEVER, ...) and plain OTel
auto-instrumentation (raw SPAN_KIND_*, connect/GET noise around one long model
call).
"""
from django.test import TestCase

from infra.ui import _rep_view, _trace_card_view


def _span(span_id, name, kind, dur_ms=0.0, **attrs):
    start = 1_700_000_000.0
    return {
        "span_id": span_id,
        "name": name,
        "kind": kind,
        "start_time": start,
        "end_time": start + dur_ms / 1000.0,
        "attributes": attrs,
    }


class TraceCardViewTest(TestCase):
    def test_none_without_trace_data(self):
        self.assertIsNone(_trace_card_view({"severity": "pass"}))
        self.assertIsNone(_trace_card_view({"judgment": {}}))

    def test_trace_ids_only(self):
        card = _trace_card_view({"trace_ids": ["a" * 32]})
        self.assertEqual(card["trace_ids"], ["a" * 32])
        self.assertEqual(card["spans"], [])
        self.assertEqual(card["total_spans"], 0)

    def test_signal_kinds_first_longest_first_capped(self):
        spans = [
            _span("s0", "chain", "CHAIN", 5.0),
            _span("s1", "llm short", "LLM", 100.0),
            _span("s2", "chain2", "CHAIN", 9.0),
        ] + [_span(f"t{i}", f"tool{i}", "TOOL", 500.0 + i) for i in range(40)]
        card = _trace_card_view({"judgment": {"evidence_spans": spans}})
        self.assertEqual(card["total_spans"], len(spans))
        # Preview capped at 15; everything else counted as hidden.
        self.assertEqual(len(card["spans"]), 15)
        self.assertEqual(card["hidden_spans"], len(spans) - 15)
        # Signal kinds come first, longest first within each group.
        self.assertEqual(card["spans"][0]["kind"], "TOOL")
        self.assertEqual(card["spans"][0]["name"], "tool39")
        self.assertEqual(card["spans"][14]["kind"], "TOOL")
        kinds = [s["kind"] for s in card["spans"]]
        self.assertNotIn("CHAIN", kinds)  # noise kinds only shown when no signal

    def test_connection_noise_is_dropped(self):
        spans = [
            _span("a", "connect", "SPAN_KIND_CLIENT", 0.0),
            _span("b", "DNS Lookup", "SPAN_KIND_CLIENT", 0.0),
            _span("c", "GET", "SPAN_KIND_CLIENT", 0.4),
            _span("d", "POST /api/v1/chat/completions", "SPAN_KIND_SERVER", 4869.0),
            _span("e", "POST https://gateway/v1/chat/completions", "SPAN_KIND_CLIENT", 4781.0),
        ]
        card = _trace_card_view({"judgment": {"evidence_spans": spans}})
        names = [s["name"] for s in card["spans"]]
        self.assertNotIn("connect", names)
        self.assertNotIn("DNS Lookup", names)
        self.assertNotIn("GET", names)
        # The meaningful operations remain, longest first.
        self.assertEqual(card["spans"][0]["name"], "POST /api/v1/chat/completions")
        self.assertEqual(card["spans"][1]["name"], "POST https://gateway/v1/chat/completions")
        self.assertEqual(card["total_spans"], 5)
        self.assertEqual(card["hidden_spans"], 0)

    def test_durations_are_exposed_for_display(self):
        card = _trace_card_view({"judgment": {"evidence_spans": [
            _span("a", "root", "SPAN_KIND_SERVER", 4869.0),
        ]}})
        self.assertEqual(card["spans"][0]["dur_ms"], 4869.0)

    def test_rep_view_exposes_trace_card(self):
        rep = {
            "severity": "high",
            "conversation": [],
            "trace_ids": ["b" * 32],
            "judgment": {"evidence_spans": [
                _span("1", "llm", "LLM", 1200.0),
            ]},
        }
        view = _rep_view(rep, 1)
        self.assertEqual(view["trace"]["trace_ids"], ["b" * 32])
        self.assertEqual(view["trace"]["spans"][0]["name"], "llm")
        # trace_ids must not leak into the raw "other" dump.
        self.assertNotIn("trace_ids", view["other"])

    def test_rep_view_no_trace_card_when_untraced(self):
        view = _rep_view({"severity": "pass", "conversation": []}, 1)
        self.assertIsNone(view["trace"])
