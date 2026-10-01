"""Trace acquisition: the Tempo provider, the factory, and the engine hand-off.

The tracing core lives in the SimpleAudit engine (``simpleaudit.tracing``).
These tests cover the studio-side layer (``infra.tracing``) and how
``infra.engine.run_scenario`` wires the engine's ``TraceCorrelation`` +
provider into a run.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_tracing
"""
from unittest import mock

from django.test import TestCase

from infra.tracing import TempoTraceProvider, build_trace_provider


def _otlp_payload(trace_id: str, spans: list[dict]) -> dict:
    """Wrap normalized-ish spans in an OTLP/HTTP JSON ExportTraceServiceRequest."""
    resource_spans = []
    for s in spans:
        attrs = [{"key": k, "value": {"stringValue": str(v)}} for k, v in (s.get("attributes") or {}).items()]
        resource_spans.append(
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": s["span_id"],
                                "parentSpanId": s.get("parent_span_id") or "",
                                "name": s.get("name", "span"),
                                "kind": 1,
                                "startTimeUnixNano": 0,
                                "endTimeUnixNano": 1,
                                "attributes": attrs,
                                "status": {"code": 0},
                            }
                        ]
                    }
                ],
            }
        )
    return {"resourceSpans": resource_spans}


def _tempo_handler(payload_by_trace: dict):
    """Build an httpx.MockTransport handler that serves Tempo's trace API.

    ``payload_by_trace`` maps trace_id -> OTLP JSON body. A trace not present
    (or mapped to ``None``) yields a 404, simulating Tempo's eventual
    consistency (not yet queryable).
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        trace_id = request.url.path.rsplit("/", 1)[-1]
        if trace_id not in payload_by_trace or payload_by_trace[trace_id] is None:
            return httpx.Response(404, json={"error": "trace not found"})
        return httpx.Response(200, json=payload_by_trace[trace_id])

    return httpx.MockTransport(handler)


class TempoProviderTest(TestCase):
    """TempoTraceProvider fetches + parses a trace from Tempo's API."""

    def _provider(self, transport, **kw):
        return TempoTraceProvider("http://tempo.local", transport=transport, retry_interval=0.01, **kw)

    def test_fetch_returns_normalized_spans(self):
        tid = "a" * 32
        payload = _otlp_payload(tid, [
            {"span_id": "1" * 16, "name": "retrieve", "attributes": {"openinference.span.kind": "RETRIEVER"}},
            {"span_id": "2" * 16, "parent_span_id": "1" * 16, "name": "llm", "attributes": {}},
        ])
        provider = self._provider(_tempo_handler({tid: payload}))
        spans = provider.fetch(tid)
        self.assertEqual(len(spans), 2)
        by_name = {s["name"]: s for s in spans}
        self.assertEqual(by_name["retrieve"]["kind"], "RETRIEVER")
        self.assertEqual(by_name["retrieve"]["trace_id"], tid)
        self.assertEqual(by_name["llm"]["parent_span_id"], "1" * 16)

    def test_fetch_retries_until_available(self):
        """A trace that 404s then appears is returned after a retry."""
        tid = "b" * 32
        payload = _otlp_payload(tid, [{"span_id": "3" * 16, "name": "tool", "attributes": {}}])
        state = {"n": 0}

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(404, json={"error": "not yet"})
            return httpx.Response(200, json=payload)

        provider = self._provider(httpx.MockTransport(handler), timeout=2.0)
        spans = provider.fetch(tid)
        self.assertEqual(len(spans), 1)
        self.assertGreaterEqual(state["n"], 2)

    def test_fetch_returns_empty_on_timeout(self):
        tid = "c" * 32
        provider = self._provider(_tempo_handler({}), timeout=0.05)
        self.assertEqual(provider.fetch(tid), [])

    def test_fetch_returns_empty_on_http_error(self):
        tid = "d" * 32
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        provider = self._provider(httpx.MockTransport(handler), timeout=0.05)
        self.assertEqual(provider.fetch(tid), [])

    def test_empty_base_url_returns_empty(self):
        provider = TempoTraceProvider("", transport=_tempo_handler({}))
        self.assertEqual(provider.fetch("e" * 32), [])

    def test_context_manager_is_noop(self):
        provider = self._provider(_tempo_handler({}))
        with provider as p:
            self.assertIs(p, provider)
        self.assertIsNone(provider.endpoint)


class BuildTraceProviderTest(TestCase):
    """build_trace_provider maps a config dict onto the right provider."""

    def test_none_or_empty_config_returns_none(self):
        self.assertIsNone(build_trace_provider(None))
        self.assertIsNone(build_trace_provider({}))

    def test_none_mode_returns_none(self):
        self.assertIsNone(build_trace_provider({"mode": "none"}))
        self.assertIsNone(build_trace_provider({"mode": "off"}))

    def test_builtin_mode_returns_builtin_otlp(self):
        from simpleaudit.tracing.provider import BuiltinOTLP

        provider = build_trace_provider({"mode": "builtin"})
        self.assertIsInstance(provider, BuiltinOTLP)

    def test_default_mode_is_builtin(self):
        from simpleaudit.tracing.provider import BuiltinOTLP

        self.assertIsInstance(build_trace_provider({"base_url": "http://x"}), BuiltinOTLP)

    def test_tempo_mode_returns_tempo_provider(self):
        provider = build_trace_provider({"mode": "tempo", "base_url": "http://tempo.local"})
        self.assertIsInstance(provider, TempoTraceProvider)

    def test_tempo_mode_requires_base_url(self):
        with self.assertRaises(ValueError):
            build_trace_provider({"mode": "tempo"})

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            build_trace_provider({"mode": "jaeger"})


class RunScenarioTracingWiringTest(TestCase):
    """run_scenario passes the correlation to the engine and attaches evidence."""

    def _snap(self, model_id, **extra):
        return {"model_id": model_id, "provider": "openai", "base_url": f"http://{model_id}.local/v1", **extra}

    def test_trace_config_records_correlation_and_attaches_evidence(self):
        from types import SimpleNamespace

        from infra.engine import run_scenario

        tid = "f" * 32
        payload = _otlp_payload(tid, [
            {"span_id": "4" * 16, "name": "retrieve", "attributes": {"openinference.span.kind": "RETRIEVER"}},
        ])
        transport = _tempo_handler({tid: payload})

        captured = {}

        class FakeAuditor:
            async def run_async(self, scenarios, **kwargs):
                captured["kwargs"] = kwargs
                correlation = kwargs.get("trace_correlation")
                if correlation is not None:
                    # Simulate the engine recording the scenario's trace id.
                    correlation.record("scen_t1", tid)
                result = SimpleNamespace(to_dict=lambda: {"severity": "pass", "judgment": {"severity": "pass"}})
                return [result]

        with mock.patch("infra.engine.build_model_auditor", return_value=(FakeAuditor(), "English")):
            payload = run_scenario(
                name="s", description="d", expected_behavior=None, test_prompt=None,
                target=self._snap("t"), auditor=self._snap("a"), judge=self._snap("j"),
                generation={"max_turns": 1},
                trace_config={
                    "mode": "tempo",
                    "base_url": "http://tempo.local",
                    "transport": transport,
                    "retry_interval": 0.01,
                },
            )

        # The engine got a correlation + audit_run_id.
        self.assertIsNotNone(captured["kwargs"].get("trace_correlation"))
        self.assertTrue(captured["kwargs"].get("audit_run_id"))
        # Evidence was fetched from the provider and attached to the judgment.
        self.assertIn("evidence_spans", payload["judgment"])
        self.assertEqual(payload["judgment"]["evidence_spans"][0]["name"], "retrieve")

    def test_no_trace_config_leaves_judgment_untouched(self):
        from types import SimpleNamespace

        from infra.engine import run_scenario

        captured = {}

        class FakeAuditor:
            async def run_async(self, scenarios, **kwargs):
                captured["kwargs"] = kwargs
                result = SimpleNamespace(to_dict=lambda: {"severity": "pass", "judgment": {"severity": "pass"}})
                return [result]

        with mock.patch("infra.engine.build_model_auditor", return_value=(FakeAuditor(), "English")):
            payload = run_scenario(
                name="s", description="d", expected_behavior=None, test_prompt=None,
                target=self._snap("t"), auditor=self._snap("a"), judge=self._snap("j"),
                generation={"max_turns": 1},
            )

        self.assertIsNone(captured["kwargs"].get("trace_correlation"))
        self.assertIsNone(captured["kwargs"].get("audit_run_id"))
        self.assertNotIn("evidence_spans", payload["judgment"])

    def test_collect_evidence_spans_none_when_no_traces(self):
        from infra.engine import _collect_evidence_spans

        self.assertIsNone(_collect_evidence_spans(None, None))

        class _NoTraces:
            def all_trace_ids(self):
                return []

        self.assertIsNone(_collect_evidence_spans(_NoTraces(), object()))
