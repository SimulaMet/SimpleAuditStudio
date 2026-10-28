"""Evidence settling, repeated-run collection, and Tempo/studio edge cases."""
from unittest import mock

import httpx
from django.test import SimpleTestCase

from infra import engine
from infra.tracing import TempoTraceProvider, enrich_trace_config


def _tool_span(span_id):
    return {
        "trace_id": "ab" * 16, "span_id": span_id, "name": "lookup", "kind": "TOOL",
        "attributes": {"openinference.span.kind": "TOOL", "gen_ai.tool.name": "acme_get_order"},
    }


class _LateExporter:
    """Provider whose spans arrive in batches after the model call returns."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.calls = 0

    def fetch(self, trace_id):
        self.calls += 1
        return self._batches.pop(0) if self._batches else []


class _Correlation:
    def all_trace_ids(self):
        return ["ab" * 16]


class SettleTest(SimpleTestCase):
    def test_late_batch_is_collected_when_settling(self):
        provider = _LateExporter([[], [_tool_span("s1")], []])
        with mock.patch.object(engine, "_SETTLE_QUIET_S", 0.01):
            evidence = engine._collect_evidence_spans(
                _Correlation(), provider, settle_timeout=5.0, structured=True,
            )
        self.assertIsNotNone(evidence)
        self.assertEqual([s["span_id"] for s in evidence["selected_spans"]], ["s1"])

    def test_no_settle_budget_skips_fetch(self):
        provider = _LateExporter([[_tool_span("s1")]])
        self.assertIsNone(engine._collect_evidence_spans(_Correlation(), provider, structured=True))
        self.assertEqual(provider.calls, 0)

    def test_settle_stops_at_budget_when_nothing_arrives(self):
        provider = _LateExporter([])
        with mock.patch.object(engine, "_SETTLE_QUIET_S", 0.01):
            self.assertIsNone(engine._collect_evidence_spans(
                _Correlation(), provider, settle_timeout=0.2, structured=True,
            ))


class StudioNoCredentialTest(SimpleTestCase):
    def test_studio_without_live_credential_runs_without_tracing(self):
        with mock.patch("infra.tracing.studio_target_id", return_value=None):
            self.assertIsNone(enrich_trace_config({"mode": "studio"}, {}))

    def test_studio_with_live_credential_resolves_target(self):
        with mock.patch("infra.tracing.studio_target_id", return_value="tgt-1"):
            self.assertEqual(
                enrich_trace_config({"mode": "studio"}, {}),
                {"mode": "studio", "target_id": "tgt-1"},
            )


class TempoBatchesTest(SimpleTestCase):
    def test_tempo_batches_key_is_parsed(self):
        payload = {"batches": [{"scopeSpans": [{"spans": [{
            "traceId": "ab" * 16, "spanId": "cd" * 8, "name": "lookup",
            "attributes": [{"key": "gen_ai.tool.name", "value": {"stringValue": "acme_get_order"}}],
        }]}]}]}

        def handler(request):
            self.assertEqual(request.headers["Accept"], "application/json")
            return httpx.Response(200, json=payload)

        provider = TempoTraceProvider(
            "http://tempo.test", timeout=1.0, retry_interval=0.01,
            transport=httpx.MockTransport(handler),
        )
        spans = provider.fetch("ab" * 16)
        self.assertEqual([s["name"] for s in spans], ["lookup"])
