"""Trace acquisition for audits — the studio-side layer over the engine's tracing API.

The SimpleAudit engine (``simpleaudit.tracing``) owns the core: the W3C
``traceparent`` correlation, the OTLP receiver, the span store, and the two
provider base classes. This module is the thin studio layer that:

* builds a provider from a run's trace config (``build_trace_provider``), and
* implements the **external** provider for Grafana Tempo (``TempoTraceProvider``),
  which the engine ships only as the abstract ``ExternalTraceProvider`` base.

Two acquisition modes (Promptfoo parity):

1. ``builtin`` — the engine's ``BuiltinOTLP`` ephemeral OTLP receiver. For
   controlled/staging targets you can point at ``provider.endpoint``.
2. ``tempo`` — ``TempoTraceProvider``. For production targets that already ship
   traces to the owner's Tempo backend; we fetch the matching trace by id.

The engine propagates the ``traceparent`` during the run and records
``turn_id -> trace_id`` in a ``TraceCorrelation``; after the run the caller
fetches spans for the recorded trace ids and hands the selected evidence to the
judge. See ``infra.engine.run_scenario``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Self

logger = logging.getLogger("simpleaudit.tracing")


class TempoTraceProvider:
    """Fetch traces from a Grafana Tempo backend by trace id.

    Subclasses the engine's ``ExternalTraceProvider`` contract (``start`` /
    ``stop`` / ``fetch``) and implements ``_fetch_remote`` against Tempo's
    trace API (``GET {base_url}/api/traces/{trace_id}``), which returns an
    OTLP/HTTP JSON ``ExportTraceServiceRequest``. The response is parsed with
    the engine's ``parse_otlp_json`` and normalized via ``SpanStore`` so the
    spans match the schema the judge's evidence selection expects.

    Tempo is eventually consistent: a trace is not queryable until its spans
    have been flushed. ``fetch`` therefore retries until the trace appears or
    ``timeout`` elapses, sleeping ``retry_interval`` between attempts.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 15.0,
        retry_interval: float = 0.5,
        headers: dict[str, str] | None = None,
        transport: Any = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout
        self._retry_interval = retry_interval
        self._headers = headers or {}
        # Injectable for tests (httpx.MockTransport); None = real network.
        self._transport = transport

    # -- TraceProvider contract (matches simpleaudit.tracing.TraceProvider) --

    def start(self) -> TempoTraceProvider:
        return self

    def stop(self) -> None:
        return None

    @property
    def endpoint(self) -> str | None:
        # Fetch-based: the target already exports to its own backend.
        return None

    def fetch(self, trace_id: str) -> list[dict[str, Any]]:
        return self._fetch_remote(trace_id)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- Tempo specifics -----------------------------------------------------

    def _fetch_remote(self, trace_id: str) -> list[dict[str, Any]]:
        """GET the trace, retrying until available or the timeout elapses."""
        if not self._base_url:
            return []
        url = f"{self._base_url}/api/traces/{trace_id}"
        deadline = time.monotonic() + self._timeout
        while True:
            spans = self._fetch_once(url)
            if spans:
                return spans
            if time.monotonic() >= deadline:
                return []
            time.sleep(self._retry_interval)

    def _fetch_once(self, url: str) -> list[dict[str, Any]]:
        """One GET + parse. Returns [] when the trace is not (yet) available.

        A 404 is Tempo's "not yet queryable" (eventual consistency) — the
        caller retries. Any other failure is logged and treated as no spans:
        tracing is best-effort evidence and must not break the run.
        """
        import httpx
        from simpleaudit.tracing.otlp import parse_otlp_json
        from simpleaudit.tracing.store import SpanStore

        try:
            client = httpx.Client(
                headers=self._headers,
                timeout=self._timeout,
                transport=self._transport,
            )
            with client:
                resp = client.get(url)
                if resp.status_code == 404:
                    return []
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - tracing must not break the run
            logger.warning("Tempo trace fetch failed for %s: %s", url, exc)
            return []

        raw_spans = parse_otlp_json(payload)
        if not raw_spans:
            return []
        store = SpanStore()
        store.add_many(raw_spans)
        return store.all()


def build_trace_provider(config: dict[str, Any] | None) -> Any | None:
    """Build a trace provider from a run's trace config, or ``None``.

    ``config`` is a dict (typically from an AuditRun's trace settings) with:

    * ``mode`` — ``"builtin"`` or ``"tempo"`` (default ``"builtin"``).
    * ``base_url`` — Tempo base URL (required for ``tempo`` mode).
    * ``timeout`` / ``retry_interval`` / ``headers`` — Tempo fetch tuning.

    Returns ``None`` when tracing is disabled (``config`` is falsy or
    ``mode`` is ``"none"``/``"off"``). The returned provider is NOT started;
    the caller manages its lifecycle (``with provider:`` or explicit
    ``start()``/``stop()``).
    """
    if not config:
        return None
    mode = str(config.get("mode") or "builtin").strip().lower()
    if mode in ("none", "off", "disabled", ""):
        return None

    if mode == "tempo":
        base_url = (config.get("base_url") or "").strip()
        if not base_url:
            raise ValueError("trace config mode='tempo' requires a non-empty base_url")
        return TempoTraceProvider(
            base_url,
            timeout=float(config.get("timeout") or 15.0),
            retry_interval=float(config.get("retry_interval") or 0.5),
            headers=config.get("headers") or None,
            transport=config.get("transport"),
        )

    if mode == "builtin":
        from simpleaudit.tracing.provider import BuiltinOTLP

        return BuiltinOTLP(
            host=config.get("host") or "127.0.0.1",
            port=int(config.get("port") or 0),
        )

    raise ValueError(f"Unknown trace mode: {mode!r} (expected 'builtin' or 'tempo')")
