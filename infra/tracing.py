"""Trace acquisition for audits — the studio-side layer over the engine's tracing API.

The SimpleAudit engine (``simpleaudit.tracing``) owns the core: the W3C
``traceparent`` correlation, the OTLP receiver, the span store, and the two
provider base classes. This module is the thin studio layer that:

* builds a provider from a run's trace config (``build_trace_provider``), and
* implements the **external** provider for Grafana Tempo (``TempoTraceProvider``),
  which the engine ships only as the abstract ``ExternalTraceProvider`` base.

Three acquisition modes (Promptfoo parity):

1. ``builtin`` — the engine's ``BuiltinOTLP`` ephemeral OTLP receiver. For
   controlled/staging targets you can point at ``provider.endpoint``.
2. ``tempo`` — ``TempoTraceProvider``. For production targets that already ship
   traces to the owner's Tempo backend; we fetch the matching trace by id.
3. ``studio`` — ``StudioTraceProvider``. For targets that export to Studio's
   own shared OTLP listener (a persistent OpenWebUI deployment); we fetch the
   matching trace by id from the listener's persisted spans.

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

# Dedicated pool for the studio span-store DB reads (see
# StudioTraceProvider._fetch_db). Never used for writes.
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from datetime import UTC

_SPAN_FETCH_POOL = _ThreadPoolExecutor(max_workers=2, thread_name_prefix="otlp-span-fetch")


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
                headers={**self._headers, "Accept": "application/json"},
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

        # Tempo's JSON trace response keys the export as "batches"; the OTLP
        # shape the parser expects is "resourceSpans".
        if isinstance(payload, dict) and "batches" in payload and "resourceSpans" not in payload:
            payload = {"resourceSpans": payload["batches"]}
        raw_spans = parse_otlp_json(payload)
        if not raw_spans:
            return []
        store = SpanStore()
        store.add_many(raw_spans)
        return store.all()


class StudioTraceProvider:
    """Fetch traces from Studio's own OTLP listener by trace id.

    The ``studio`` mode for targets that export to the *shared* Studio OTLP
    endpoint (``POST /otlp/v1/traces``) — e.g. a persistent OpenWebUI
    deployment whose ``OTEL_EXPORTER_OTLP_ENDPOINT`` is fixed at boot and cannot
    be re-pointed at a per-run ``builtin`` ephemeral receiver. The engine
    propagates a W3C ``traceparent`` on the run's requests; the target links
    its spans to it and pushes them to the listener, which persists them
    (``model_registry.models.OtlpSpan``) tagged with the target's credential
    ``target_id``. ``fetch`` reads those rows back by ``(target_id, trace_id)``
    — the join that lets a separate worker process read what the web process
    ingested, in the same "fetch by id, retry until available" shape as
    :class:`TempoTraceProvider`.
    """

    def __init__(
        self,
        target_id: str,
        *,
        timeout: float = 15.0,
        retry_interval: float = 0.5,
        fetcher: Any = None,
    ) -> None:
        self._target_id = (target_id or "").strip()
        self._timeout = timeout
        self._retry_interval = retry_interval
        # Injectable for tests; defaults to the DB-backed listener read.
        self._fetcher = fetcher or self._fetch_db

    # -- TraceProvider contract (matches simpleaudit.tracing.TraceProvider) --

    def start(self) -> StudioTraceProvider:
        return self

    def stop(self) -> None:
        return None

    @property
    def endpoint(self) -> str | None:
        # Fetch-based: the target already exports to the shared listener.
        return None

    def fetch(self, trace_id: str) -> list[dict[str, Any]]:
        """Spans for ``trace_id``, retrying until available or the timeout elapses."""
        if not self._target_id or not trace_id:
            return []
        deadline = time.monotonic() + self._timeout
        while True:
            spans = self._fetcher(self._target_id, trace_id)
            if spans:
                return spans
            if time.monotonic() >= deadline:
                return []
            time.sleep(self._retry_interval)

    def fetch_window(self, start_ts: float, end_ts: float) -> list[dict[str, Any]]:
        """Spans received during [start_ts, end_ts] for this target.

        Fallback for targets that do not propagate the W3C traceparent
        (e.g. OpenWebUI generates its own trace IDs). Matches by target_id
        and the wall-clock window the run executed in. Filters out raw
        HTTP transport spans that carry no agent-level semantics.

        The Django ORM read must run on a sync thread: the engine calls this
        from inside its asyncio loop (per-scenario evidence collection), and
        a synchronous ORM query on a thread with a live event loop raises
        SynchronousOnlyOperation. Mirrors ``_fetch_db``'s thread hop.
        """
        if not self._target_id:
            return []
        from datetime import datetime

        def _read() -> list[dict[str, Any]]:
            from django.db import close_old_connections

            from model_registry.models import OtlpSpan

            close_old_connections()  # the pool thread's connection may be stale
            dt_start = datetime.fromtimestamp(start_ts, tz=UTC)
            dt_end = datetime.fromtimestamp(end_ts, tz=UTC)
            rows = OtlpSpan.objects.filter(
                target_id=self._target_id,
                received_at__gte=dt_start,
                received_at__lte=dt_end,
            ).order_by("received_at")
            results = []
            for r in rows:
                attrs = r.attributes or {}
                # Only keep OpenInference semantic spans (agent-level tool
                # calls, LLM completions, workflow chains).  Raw OTel
                # HTTP/ASGI spans don't carry this attribute and are pure
                # transport noise.
                if "openinference.span.kind" not in attrs:
                    continue
                results.append({
                    "trace_id": r.trace_id,
                    "span_id": r.span_id,
                    "name": r.name,
                    "kind": r.kind,
                    "parent_span_id": r.parent_span_id or "",
                    "start_time": r.start_time or 0,
                    "end_time": r.end_time or 0,
                    "status": r.status,
                    "attributes": r.attributes or {},
                })
            return results

        import asyncio

        try:
            in_loop = asyncio.get_running_loop() is not None
        except RuntimeError:
            in_loop = False

        try:
            if in_loop:
                return _SPAN_FETCH_POOL.submit(_read).result(timeout=30)
            return _read()
        except Exception as exc:  # noqa: BLE001 - tracing must not break the run
            logger.warning("Studio span window fetch failed for %s: %s", self._target_id, exc)
            return []

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- Studio listener specifics -------------------------------------------

    @staticmethod
    def _fetch_db(target_id: str, trace_id: str) -> list[dict[str, Any]]:
        """One read of the listener's persisted spans (no retry here).

        The engine can call ``fetch`` from inside its asyncio loop (per-turn
        evidence collection). The Django ORM cannot run on a thread with a
        live event loop (``SynchronousOnlyOperation``), so when a loop is
        running the DB read is pushed onto a dedicated sync thread; from a
        plain sync context it runs inline (which also keeps transactional
        tests, whose rows are only visible on the test thread, working).
        """
        from model_registry.otlp_views import get_spans_for_trace_db

        def _read() -> list[dict[str, Any]]:
            from django.db import close_old_connections

            close_old_connections()  # the pool thread's connection may be stale
            return get_spans_for_trace_db(target_id, trace_id)

        import asyncio

        try:
            in_loop = asyncio.get_running_loop() is not None
        except RuntimeError:
            in_loop = False

        try:
            if in_loop:
                # Shared executor: the sync read is isolated from the engine's
                # loop thread.
                return _SPAN_FETCH_POOL.submit(_read).result(timeout=30)
            return _read()
        except Exception as exc:  # noqa: BLE001 - tracing must not break the run
            logger.warning("Studio span fetch failed for %s/%s: %s", target_id, trace_id, exc)
            return []


def studio_target_id(target_snapshot: dict[str, Any] | None) -> str | None:
    """The OTLP credential ``target_id`` for a run's frozen target, if any.

    ``target_snapshot`` is a run's frozen ``target_config_snapshot`` (it carries
    ``connection_id``). Returns the enabled credential's ``target_id`` when the
    target's connection has one — i.e. the target is configured to export OTLP
    to Studio — else ``None`` (nothing to trace).
    """
    if not target_snapshot:
        return None
    from model_registry.models import OTLPCredential

    conn_id = target_snapshot.get("connection_id")
    if not conn_id:
        return None
    return (
        OTLPCredential.objects.filter(connection_id=conn_id, enabled=True)
        .order_by("-created_at")
        .values_list("target_id", flat=True)
        .first()
    )


def enrich_trace_config(trace_config: dict[str, Any] | None, target_snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resolve the ``studio`` mode's ``target_id`` from a run's frozen target.

    The frozen config only carries the *intent* (``mode: studio``); the
    ``target_id`` is re-resolved at execution time from the run's frozen target
    snapshot, so the provider always fetches the *live* credential's spans even
    when the credential was created or rotated after the run was queued (the
    spans arriving now carry the current credential's target tag). Non-studio
    Non-studio configs (and ``None``) pass through untouched (identity, no
    mutation); a studio config returns a copy with the live ``target_id``.
    """
    if not trace_config or str(trace_config.get("mode") or "").strip().lower() != "studio":
        return trace_config
    target_id = studio_target_id(target_snapshot)
    if not target_id:
        # The credential was revoked or deleted after the run was queued. Run
        # without trace evidence rather than failing the whole audit.
        logger.warning("Studio trace target has no enabled OTLP credential; running without traces")
        return None
    return {**trace_config, "target_id": target_id}


def build_trace_provider(config: dict[str, Any] | None) -> Any | None:
    """Build a trace provider from a run's trace config, or ``None``.

    ``config`` is a dict (typically from an AuditRun's trace settings) with:

    * ``mode`` — ``"builtin"``, ``"tempo"``, or ``"studio"`` (default ``"builtin"``).
    * ``base_url`` — Tempo base URL (required for ``tempo`` mode).
    * ``target_id`` — the OTLP credential ``target_id`` (required for ``studio``
      mode; the run's target must have an OTLP credential on its connection).
    * ``timeout`` / ``retry_interval`` / ``headers`` — fetch tuning.

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

    if mode == "studio":
        target_id = (config.get("target_id") or "").strip()
        if not target_id:
            raise ValueError("trace config mode='studio' requires a non-empty target_id")
        # Use a short per-trace-id fetch timeout so the settle loop isn't
        # blocked: targets that don't propagate W3C traceparent (e.g. OWUI)
        # will never match by trace ID, and the window fallback handles them.
        return StudioTraceProvider(
            target_id,
            timeout=float(config.get("timeout") or 3.0),
            retry_interval=float(config.get("retry_interval") or 0.5),
            fetcher=config.get("fetcher"),
        )

    if mode == "builtin":
        from simpleaudit.tracing.provider import BuiltinOTLP

        return BuiltinOTLP(
            host=config.get("host") or "127.0.0.1",
            port=int(config.get("port") or 0),
        )

    raise ValueError(f"Unknown trace mode: {mode!r} (expected 'builtin', 'studio', or 'tempo')")
