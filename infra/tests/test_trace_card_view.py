"""The result page's Target trace card: _rep_view surfaces the rep's trace ids
and the captured evidence spans.

The card must stay readable for both flavors of target instrumentation:
OpenInference-style spans (semantic kinds: LLM, RETRIEVER, ...) and plain OTel
auto-instrumentation (raw SPAN_KIND_*, connect/GET noise around one long model
call).
"""
from django.test import SimpleTestCase, TestCase

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

    def test_offset_ms_is_time_since_first_span(self):
        spans = [
            _span("late", "late", "LLM", 100.0),
            _span("early", "early", "TOOL", 50.0),
        ]
        spans[0]["start_time"] = 1_700_000_010.0  # +10s
        card = _trace_card_view({"judgment": {"evidence_spans": spans}})
        offsets = {s["name"]: s["offset_ms"] for s in card["spans"]}
        self.assertEqual(offsets["early"], 0.0)
        self.assertEqual(offsets["late"], 10_000.0)

    def test_content_blocks_surface_tool_args_and_result(self):
        spans = [
            _span(
                "t1", "lookup", "TOOL", 50.0,
                **{
                    "gen_ai.tool.name": "acme_get_order",
                    "gen_ai.tool.call.arguments": '{"order_id": "ACME-1001"}',
                    "gen_ai.tool.call.result": '{"status": "shipped"}',
                },
            ),
            _span(
                "l1", "llm", "LLM", 100.0,
                **{"input.value": "hi", "output.value": "hello"},
            ),
            _span("x1", "plain", "SPAN_KIND_INTERNAL", 10.0, **{"http.method": "GET"}),
        ]
        card = _trace_card_view({"judgment": {"evidence_spans": spans}})
        blocks = {s["name"]: s["content_blocks"] for s in card["spans"]}
        self.assertEqual(
            blocks["lookup"],
            [
                ("Tool arguments", '{"order_id": "ACME-1001"}'),
                ("Tool result", '{"status": "shipped"}'),
            ],
        )
        self.assertEqual(blocks["llm"], [("Input", "hi"), ("Output", "hello")])
        self.assertEqual(blocks["plain"], [])

    def test_rep_view_tolerates_non_numeric_provider_token_values(self):
        view = _rep_view(
            {
                "severity": "medium",
                "conversation": [],
                "target_input_tokens": "[REDACTED]",
                "target_output_tokens": "unknown",
                "judge_input_tokens": "12",
                "judge_output_tokens": 8,
            },
            1,
        )
        self.assertEqual(view["total_tokens"], 20)


class DuplicateInferenceTests(SimpleTestCase):
    def _llm(self, span_id, trace, start, messages):
        return {"span_id": span_id, "trace_id": trace, "kind": "LLM", "start_time": start,
                "attributes": {"gen_ai.input.messages": messages}}

    def test_identical_consecutive_model_calls_in_one_trace_are_flagged(self):
        from infra.ui import _duplicate_inference_flags

        spans = [self._llm("a", "t1", 1.0, "same"), self._llm("b", "t1", 2.0, "same")]
        self.assertEqual(list(_duplicate_inference_flags(spans)), [1])

    def test_different_input_or_different_trace_is_not_flagged(self):
        from infra.ui import _duplicate_inference_flags

        spans = [
            self._llm("a", "t1", 1.0, "first"),
            self._llm("b", "t1", 2.0, "second"),
            self._llm("c", "t2", 3.0, "second"),
        ]
        self.assertEqual(_duplicate_inference_flags(spans), {})

    def test_spans_without_input_are_ignored(self):
        from infra.ui import _duplicate_inference_flags

        spans = [self._llm("a", "t1", 1.0, ""), self._llm("b", "t1", 2.0, "")]
        self.assertEqual(_duplicate_inference_flags(spans), {})
