"""The result page's Target trace card: _rep_view surfaces the rep's trace ids
and the captured evidence spans."""
from django.test import TestCase

from infra.ui import _rep_view, _trace_card_view


class TraceCardViewTest(TestCase):
    def test_none_without_trace_data(self):
        self.assertIsNone(_trace_card_view({"severity": "pass"}))
        self.assertIsNone(_trace_card_view({"judgment": {}}))

    def test_trace_ids_only(self):
        card = _trace_card_view({"trace_ids": ["a" * 32]})
        self.assertEqual(card["trace_ids"], ["a" * 32])
        self.assertEqual(card["spans"], [])
        self.assertEqual(card["total_spans"], 0)

    def test_spans_prefers_signal_kinds_and_caps_preview(self):
        spans = [{"span_id": str(i), "name": f"s{i}", "kind": k}
                 for i, k in enumerate(["CHAIN", "LLM", "CHAIN"] + ["TOOL"] * 40)]
        judgment = {"evidence_spans": spans}
        card = _trace_card_view({"judgment": judgment})
        self.assertEqual(card["total_spans"], len(spans))
        # Signal kinds (LLM/TOOL) shown first, capped at 30, rest counted.
        self.assertEqual(len(card["spans"]), 30)
        self.assertEqual(card["hidden_spans"], len(spans) - 30)
        self.assertEqual(card["spans"][0]["kind"], "LLM")

    def test_rep_view_exposes_trace_card(self):
        rep = {
            "severity": "high",
            "conversation": [],
            "trace_ids": ["b" * 32],
            "judgment": {"evidence_spans": [
                {"span_id": "1", "name": "llm", "kind": "LLM"},
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
