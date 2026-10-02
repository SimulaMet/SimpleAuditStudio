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

    def test_studio_mode_returns_studio_provider(self):
        from infra.tracing import StudioTraceProvider

        provider = build_trace_provider({"mode": "studio", "target_id": "tgt_1"})
        self.assertIsInstance(provider, StudioTraceProvider)

    def test_studio_mode_requires_target_id(self):
        with self.assertRaises(ValueError):
            build_trace_provider({"mode": "studio"})


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
        # The scenario's W3C trace ids are persisted on the result for the UI.
        self.assertEqual(payload["trace_ids"], [tid])

    def test_studio_mode_end_to_end_fetches_listener_spans(self):
        """studio mode: the provider reads the listener's persisted OtlpSpan
        rows, so the run attaches exactly the target's exported evidence."""
        from types import SimpleNamespace

        from infra.engine import run_scenario
        from model_registry import otlp_views
        from model_registry.models import OtlpSpan

        tid = "5" * 32
        OtlpSpan.objects.create(
            target_id="sim_tgt", trace_id=tid, span_id="9" * 16,
            name="chat completion", kind="LLM",
            attributes={"openinference.span.kind": "LLM", "simpleaudit.target_id": "sim_tgt"},
        )
        # A span the target exported under a *different* trace must not attach.
        OtlpSpan.objects.create(target_id="sim_tgt", trace_id="6" * 32, span_id="a" * 15 + "1", name="other", kind="LLM")

        captured = {}

        class FakeAuditor:
            async def run_async(self, scenarios, **kwargs):
                captured["kwargs"] = kwargs
                correlation = kwargs.get("trace_correlation")
                if correlation is not None:
                    correlation.record("scen_t1", tid)
                result = SimpleNamespace(to_dict=lambda: {"severity": "pass", "judgment": {"severity": "pass"}})
                return [result]

        with mock.patch("infra.engine.build_model_auditor", return_value=(FakeAuditor(), "English")):
            payload = run_scenario(
                name="s", description="d", expected_behavior=None, test_prompt=None,
                target=self._snap("t"), auditor=self._snap("a"), judge=self._snap("j"),
                generation={"max_turns": 1},
                trace_config={
                    "mode": "studio",
                    "target_id": "sim_tgt",
                    "timeout": 1.0,
                    "retry_interval": 0.01,
                },
            )

        spans = payload["judgment"]["evidence_spans"]
        self.assertEqual([s["name"] for s in spans], ["chat completion"])
        self.assertEqual(payload["trace_ids"], [tid])
        # The listener read went through the DB-backed fetcher.
        self.assertEqual(otlp_views.get_spans_for_trace_db("sim_tgt", tid)[0]["span_id"], "9" * 16)

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


class StudioTraceProviderTest(TestCase):
    """StudioTraceProvider reads the run's spans from the listener's persisted
    OtlpSpan rows by (target_id, trace_id), retrying until available."""

    def test_fetch_returns_persisted_spans(self):
        from model_registry import otlp_views
        from model_registry.models import OtlpSpan

        tid = "7" * 32
        from infra.tracing import StudioTraceProvider

        OtlpSpan.objects.bulk_create([
            OtlpSpan(target_id="tgt", trace_id=tid, span_id="a" * 16, name="llm chat", kind="LLM",
                     start_time=1.0, end_time=2.0, attributes={"openinference.span.kind": "LLM"}),
            OtlpSpan(target_id="tgt", trace_id=tid, span_id="b" * 16, name="retrieval", kind="RETRIEVER",
                     start_time=0.5, end_time=1.0),
        ])
        # A different trace / target must not leak in.
        OtlpSpan.objects.create(target_id="other", trace_id=tid, span_id="c" * 16, name="nope")
        OtlpSpan.objects.create(target_id="tgt", trace_id="9" * 32, span_id="d" * 16, name="nope2")

        provider = StudioTraceProvider("tgt", retry_interval=0.01, fetcher=otlp_views.get_spans_for_trace_db)
        spans = provider.fetch(tid)
        self.assertEqual({s["name"] for s in spans}, {"llm chat", "retrieval"})
        # Ordering: start_time then span_id.
        self.assertEqual([s["name"] for s in spans], ["retrieval", "llm chat"])

    def test_fetch_retries_until_available(self):
        from infra.tracing import StudioTraceProvider

        tid = "8" * 32
        calls = {"n": 0}

        def fetcher(target_id, trace_id):
            calls["n"] += 1
            if calls["n"] < 3:
                return []
            return [{"span_id": "e" * 16, "trace_id": tid, "name": "late", "kind": "TOOL"}]

        provider = StudioTraceProvider("tgt", timeout=5.0, retry_interval=0.01, fetcher=fetcher)
        spans = provider.fetch(tid)
        self.assertEqual(len(spans), 1)
        self.assertGreaterEqual(calls["n"], 3)

    def test_fetch_returns_empty_on_timeout(self):
        from infra.tracing import StudioTraceProvider

        provider = StudioTraceProvider("tgt", timeout=0.05, retry_interval=0.01, fetcher=lambda t, tid: [])
        self.assertEqual(provider.fetch("1" * 32), [])

    def test_fetch_empty_target_or_trace_id(self):
        from infra.tracing import StudioTraceProvider

        provider = StudioTraceProvider("", fetcher=lambda t, tid: [{"x": 1}])
        self.assertEqual(provider.fetch("2" * 32), [])
        provider = StudioTraceProvider("tgt", fetcher=lambda t, tid: [{"x": 1}])
        self.assertEqual(provider.fetch(""), [])

    def test_default_fetcher_reads_db(self):
        """With no injected fetcher the provider reads the OtlpSpan table."""
        from model_registry.models import OtlpSpan

        tid = "3" * 32
        OtlpSpan.objects.create(target_id="tgt", trace_id=tid, span_id="f" * 16, name="db span", kind="AGENT")
        from infra.tracing import StudioTraceProvider

        provider = StudioTraceProvider("tgt", retry_interval=0.01)
        self.assertEqual([s["name"] for s in provider.fetch(tid)], ["db span"])


class EnrichTraceConfigTest(TestCase):
    """The worker re-resolves studio mode's target_id from the frozen target."""

    def test_studio_mode_resolves_credential_target_id(self):
        from infra.tests.factories import (
            ModelConnectionFactory,
            ProjectFactory,
            UserFactory,
        )
        from infra.tracing import enrich_trace_config
        from model_registry import otlp_services as otlp

        project = ProjectFactory()
        conn = ModelConnectionFactory(project=project, name="owui")
        user = UserFactory()
        nc = otlp.create_credential(project=project, connection=conn, auth_mode="basic", user=user)

        enriched = enrich_trace_config(
            {"mode": "studio"}, {"connection_id": conn.id, "model_id": "m"}
        )
        self.assertEqual(enriched["mode"], "studio")
        self.assertEqual(enriched["target_id"], nc.credential.target_id)

    def test_studio_mode_no_credential_empty_target_id(self):
        from infra.tests.factories import ModelConnectionFactory, ProjectFactory
        from infra.tracing import enrich_trace_config

        project = ProjectFactory()
        conn = ModelConnectionFactory(project=project, name="plain")
        enriched = enrich_trace_config({"mode": "studio"}, {"connection_id": conn.id})
        self.assertEqual(enriched["target_id"], "")

    def test_non_studio_config_passthrough(self):
        from infra.tracing import enrich_trace_config

        tempo = {"mode": "tempo", "base_url": "http://tempo.local"}
        self.assertIs(enrich_trace_config(tempo, {"connection_id": 1}), tempo)
        self.assertIsNone(enrich_trace_config(None, {"connection_id": 1}))
        empty = {}
        self.assertIs(enrich_trace_config(empty, None), empty)
