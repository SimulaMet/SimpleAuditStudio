"""OTLP ingestion + credential management for Studio.

Two kinds of endpoints live here:

1. **Ingestion** — ``POST /otlp/v1/traces``. Machine-to-machine: an external
   target (OpenWebUI, an agent, a framework) pushes OTLP/HTTP-JSON spans. It is
   authenticated by the ``Authorization`` header (Basic or Bearer) against the
   workspace's ``OTLPCredential`` rows, NOT by a Studio session. On success the
   spans are tagged with the credential's ``target_id`` and stored.

2. **Credential management** — session-authenticated DRF views under
   ``/api/otlp/credentials/`` that let a workspace admin create, list, and
   revoke credentials (and fetch the copy-paste env vars for a target).
"""
from __future__ import annotations

import atexit
import base64
import gzip
import io
import json
import logging
import threading
import time

from django.conf import settings
from django.db import close_old_connections
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from simpleaudit.tracing.store import SpanStore, normalize_span

from audits.agentic.auditable import select_auditable_spans
from infra.exceptions import StableAPIError
from model_registry import otlp_config
from model_registry import otlp_services as otlp
from model_registry.models import ModelConnection, OTLPCredential, OtlpSpan

# In-memory span store for the shared receiver, keyed by target_id. Each bucket
# is an engine ``SpanStore`` (the same normalized schema the run path and the
# judge's evidence selection use), so spans ingested here are directly
# consumable by ``select_spans``. (A persistent backend can replace this
# without changing the endpoint contract.)
_SPAN_STORE: dict[str, SpanStore] = {}
IN_MEMORY_SPAN_CAP = 20_000  # per target; the DB holds the durable copy


# Per-target receipt log for the "Test OTLP" button: every authenticated export
# is noted here, including ones whose spans are all filtered out, so a user can
# tell "nothing arrives" from "arrives but nothing auditable". Per process.
_LAST_EXPORT: dict[str, dict] = {}


def note_export(target_id: str, received: int, kept: int) -> None:
    prev = _LAST_EXPORT.get(target_id) or {"exports": 0, "received": 0, "kept": 0}
    _LAST_EXPORT[target_id] = {
        "at": time.time(),
        "exports": prev["exports"] + 1,
        "received": prev["received"] + received,
        "kept": prev["kept"] + kept,
    }


def last_export(target_id: str) -> dict | None:
    return _LAST_EXPORT.get(target_id)


def _store_for_target(target_id: str) -> SpanStore:
    store = _SPAN_STORE.get(target_id)
    if store is None:
        store = SpanStore()
        _SPAN_STORE[target_id] = store
    return store


def get_spans_for_target(target_id: str) -> list[dict]:
    """All normalized spans ingested for ``target_id`` (for a run to fetch evidence)."""
    store = _SPAN_STORE.get(target_id)
    return store.all() if store else []


def get_spans_for_trace(target_id: str, trace_id: str) -> list[dict]:
    """Spans ingested for ``target_id`` belonging to ``trace_id``.

    This is the cross-process join the studio trace mode uses: the target
    already exports to this listener at boot, and a run's W3C ``traceparent``
    ties its turns' spans to the run's trace ids, so a worker (even a separate
    one) can fetch exactly this run's spans by id.
    """
    store = _SPAN_STORE.get(target_id)
    if store is None or not trace_id:
        return []
    return store.by_trace(trace_id)


def get_spans_for_trace_db(target_id: str, trace_id: str) -> list[dict]:
    """Spans persisted for ``target_id`` + ``trace_id`` (cross-process read).

    The in-memory store above is per-process: the web/API process ingests spans
    while a *separate* Hatchet worker runs the audit. The DB row is what lets
    the worker read what the target exported, so this is the source the studio
    trace mode's provider actually fetches from in production.

    The read is also the claim: asking for a trace keeps its spans (held ones
    are promoted) and later arrivals for it are stored without a hold.
    """
    if not trace_id:
        return []
    flush_pending(target_id)
    otlp.claim_traces(target_id, {trace_id})
    rows = OtlpSpan.objects.filter(target_id=target_id, trace_id=trace_id).order_by("start_time", "span_id")
    return [
        {
            "span_id": r.span_id,
            "trace_id": r.trace_id,
            "name": r.name,
            "kind": r.kind,
            "parent_span_id": r.parent_span_id,
            "start_time": r.start_time,
            "end_time": r.end_time,
            "status": r.status,
            "attributes": r.attributes,
        }
        for r in rows
    ]


# Map GenAI operations to internal audit categories. The OTLP proto span kind
# (CLIENT/SERVER/INTERNAL) describes transport and must not classify AI steps.
_OPERATION_NAME_TO_KIND = {
    "chat": "LLM",
    "generate_content": "LLM",
    "text_completion": "LLM",
    "execute_tool": "TOOL",
    "invoke_agent": "AGENT",
    "invoke_workflow": "AGENT",
    "retrieval": "RETRIEVER",
    "embeddings": "EMBEDDING",
    "embedding": "EMBEDDING",
    "rerank": "RERANKER",
}


def _refine_span_kind(span: dict) -> str:
    """Choose an internal audit kind solely from GenAI attributes."""
    attrs = span.get("attributes") or {}
    operation = str(attrs.get("gen_ai.operation.name") or "").lower()
    if operation in _OPERATION_NAME_TO_KIND:
        return _OPERATION_NAME_TO_KIND[operation]
    if attrs.get("gen_ai.tool.name"):
        return "TOOL"
    if attrs.get("gen_ai.retrieval.documents"):
        return "RETRIEVER"
    return "CHAIN"


def _stamp_refined_kind(raw_spans: list[dict]) -> None:
    """Set each raw span's ``kind`` to its refined value, in place.

    ``normalize_span`` reads ``raw.get("kind")`` (as a string) when no explicit
    span-kind attribute is present, so stamping here makes BOTH the in-memory
    SpanStore (which normalizes internally) and the DB persistence path agree
    on the kind without either needing its own refinement pass.
    """
    for span in raw_spans:
        span["kind"] = _refine_span_kind(span)


# Write-behind buffer. Ingest adds rows here; a one-shot timer writes everything
# buffered in one bulk upsert per target, so DB writes scale with time, not with
# export requests. Reads in this process flush first, so they see their own writes.
# Spans in the buffer are not visible to other processes until flushed (at most
# ``OTLP_SPAN_FLUSH_INTERVAL_S`` later) and are lost if the process dies first.
_PENDING: dict[tuple[str, str, str], OtlpSpan] = {}
_PENDING_LOCK = threading.Lock()
_flush_timer: threading.Timer | None = None
MAX_PENDING_SPANS = 50_000


def _flush_interval() -> float:
    return float(getattr(settings, "OTLP_SPAN_FLUSH_INTERVAL_S", 1.0))


def buffer_has_room(incoming: int) -> bool:
    """Whether ``incoming`` more spans fit in the write buffer right now.

    The receiver checks this before accepting an export: a full buffer answers
    429 so the exporter retries, instead of acknowledging spans we then drop.
    """
    if _flush_interval() <= 0:
        return True
    with _PENDING_LOCK:
        return len(_PENDING) + incoming <= MAX_PENDING_SPANS


def _buffer_spans(rows: list[OtlpSpan]) -> None:
    interval = _flush_interval()
    if interval <= 0:
        _write_rows(rows)
        return
    global _flush_timer
    with _PENDING_LOCK:
        for row in rows:
            _PENDING[(row.target_id, row.trace_id, row.span_id)] = row
        overflow = len(_PENDING) - MAX_PENDING_SPANS
        for key in list(_PENDING)[:max(overflow, 0)]:
            del _PENDING[key]
        if overflow > 0:
            logging.getLogger(__name__).warning("OTLP span buffer full: dropped %d oldest spans", overflow)
        if _flush_timer is None:
            _flush_timer = threading.Timer(interval, _timer_flush)
            _flush_timer.daemon = True
            _flush_timer.start()


def flush_pending(target_id: str | None = None) -> None:
    """Write buffered spans (all targets, or one) to the DB now."""
    with _PENDING_LOCK:
        keys = [k for k in _PENDING if target_id is None or k[0] == target_id]
        batch = [_PENDING.pop(k) for k in keys]
    if not batch:
        return
    try:
        _write_rows(batch)
    except Exception:
        logging.getLogger(__name__).exception("OTLP span flush failed; will retry")
        with _PENDING_LOCK:
            for row in batch:
                _PENDING.setdefault((row.target_id, row.trace_id, row.span_id), row)


def _timer_flush() -> None:
    global _flush_timer
    try:
        close_old_connections()
        flush_pending()
    finally:
        with _PENDING_LOCK:
            _flush_timer = None
            retry = bool(_PENDING)
        if retry:
            _buffer_spans([])  # re-arm the timer for the rows that failed
        close_old_connections()


atexit.register(flush_pending)


def _write_rows(rows: list[OtlpSpan]) -> None:
    """Upsert buffered rows. Held or kept is decided per trace at write time."""
    by_target: dict[str, list[OtlpSpan]] = {}
    for row in rows:
        by_target.setdefault(row.target_id, []).append(row)
    hold_until = timezone.now() + otlp.SPAN_HOLD_TTL
    for target_id, group in by_target.items():
        trace_ids = {row.trace_id for row in group}
        claimed = otlp.claimed_trace_ids(target_id, trace_ids)
        if otlp.SPAN_HOLD_TTL.total_seconds() <= 0:
            # No hold window: a span whose trace no run has claimed is not stored.
            group = [row for row in group if row.trace_id in claimed]
        for row in group:
            row.held_until = None if row.trace_id in claimed else hold_until
        # held_until is not in update_fields: a conflicting row keeps the state it
        # already has, so a promoted (claimed) span is never re-held by a repeat.
        OtlpSpan.objects.bulk_create(
            group,
            batch_size=500,
            update_conflicts=True,
            unique_fields=["target_id", "trace_id", "span_id"],
            update_fields=["name", "kind", "parent_span_id", "start_time", "end_time", "status", "attributes"],
        )
        # A claim may land between the check above and the insert: promote again.
        otlp.promote_claimed_spans(target_id, trace_ids)


def _persist_spans(target_id: str, raw_spans: list[dict], capture_level: str = "structural") -> None:
    """Normalize an export and queue its spans for the write-behind buffer.

    Rows are upserted later, in bulk, by ``flush_pending`` (keyed on target,
    trace and span id). Queueing never fails the OTLP export.
    """
    from audits.agentic.privacy import sanitize_span

    rows: dict[tuple[str, str, str], OtlpSpan] = {}
    for raw in raw_spans:
        raw = sanitize_span(raw, capture_level)
        span = normalize_span(raw)
        span_id = span.get("span_id") or ""
        trace_id = span.get("trace_id") or ""
        # Without a trace id no run can ever claim the span: nothing to keep.
        if not span_id or not trace_id:
            continue
        # A request may repeat a span: keep the last copy, as an upsert would.
        rows[(target_id, trace_id, span_id)] = OtlpSpan(
            target_id=target_id,
            trace_id=trace_id,
            span_id=span_id,
            name=(span.get("name") or "span")[:500],
            kind=_refine_span_kind(span)[:50],
            parent_span_id=span.get("parent_span_id"),
            start_time=span.get("start_time"),
            end_time=span.get("end_time"),
            status=(span.get("status") or "OK")[:20],
            attributes=span.get("attributes") or {},
        )
    _buffer_spans(list(rows.values()))


def _trim_store(target_id: str) -> None:
    """Keep the in-memory bucket to its newest ``IN_MEMORY_SPAN_CAP`` spans."""
    store = _SPAN_STORE.get(target_id)
    if store is None or len(store) <= IN_MEMORY_SPAN_CAP:
        return
    kept = SpanStore()
    kept.add_many(store.all()[-IN_MEMORY_SPAN_CAP:])
    _SPAN_STORE[target_id] = kept


def clear_target_spans(target_id: str) -> None:
    _SPAN_STORE.pop(target_id, None)


# ─── Ingestion (machine-to-machine) ─────────────────────────────────────────


def _otlp_body_to_dict(body: bytes, content_type: str | None) -> dict:
    """Decode an OTLP trace-export body into the OTLP/HTTP-JSON dict shape.

    Returns a dict parseable by :func:`simpleaudit.tracing.otlp.parse_otlp_json`
    (int64 timestamps stay strings; that module already handles both int and
    string nanosecond values).
    """
    ctype = (content_type or "").lower()
    if "protobuf" in ctype or "octet-stream" in ctype:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        req = ExportTraceServiceRequest()
        req.ParseFromString(body)
        d = MessageToDict(req, preserving_proto_field_name=False)
        # protobuf JSON encodes bytes (traceId/spanId/parentSpanId) as base64,
        # while the OTLP/HTTP JSON spec uses lowercase hex. The rest of the
        # pipeline (TraceCorrelation, traceparent, OtlpSpan) is hex-based, so
        # normalize to hex here.
        _hexify_id_fields(d)
        return d
    return json.loads(body)


_STATUS_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}


def _normalize_span_status(payload: dict) -> None:
    """Map OTLP status names to codes and keep an error's exception message.

    Protobuf exports arrive as ``STATUS_CODE_ERROR`` strings, which the engine's
    parser does not recognize (it treats them as OK). The exception text lives
    in the span's ``exception`` event, which the parser drops, so it is copied
    onto the span's attributes first.
    """
    for resource in payload.get("resourceSpans") or []:
        for scope in resource.get("scopeSpans") or []:
            for span in scope.get("spans") or []:
                status = span.get("status") or {}
                code = status.get("code")
                if isinstance(code, str):
                    code = _STATUS_CODES.get(code, 0)
                    status["code"] = code
                    span["status"] = status
                if code != 2:
                    continue
                attributes = span.setdefault("attributes", [])
                for event in span.get("events") or []:
                    if event.get("name") != "exception":
                        continue
                    for kv in event.get("attributes") or []:
                        if kv.get("key") in ("exception.type", "exception.message"):
                            attributes.append(kv)


def _hexify_id_fields(node) -> None:
    """Recursively convert protobuf-JSON base64 id fields to hex in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("traceId", "spanId", "parentSpanId") and isinstance(value, str) and value:
                try:
                    node[key] = base64.b64decode(value).hex()
                except Exception:  # noqa: BLE001,S110 - not base64 (already hex); leave as-is
                    pass
            else:
                _hexify_id_fields(value)
    elif isinstance(node, list):
        for item in node:
            _hexify_id_fields(item)


def _bounded_gunzip(body: bytes, limit: int) -> bytes | None:
    """Gunzip ``body``; ``None`` if it inflates past ``limit`` bytes."""
    out = gzip.GzipFile(fileobj=io.BytesIO(body)).read(limit + 1)
    return None if len(out) > limit else out


@csrf_exempt
def otlp_traces(request):
    """Accept an OTLP/HTTP-JSON trace export from an authenticated target.

    Auth: ``Authorization: Basic base64(user:pass)`` or ``Bearer <token>``.
    Returns the OTLP ack (200) on success, 401 on auth failure, 405 on a
    non-POST. Spans are tagged with the credential's ``target_id``.

    Accepts both OTLP/HTTP wire formats — ``http/json`` and ``http/protobuf``.
    OpenWebUI's exporter picks the wire format from ``OTEL_OTLP_SPAN_EXPORTER``
    and ignores ``OTEL_EXPORTER_OTLP_PROTOCOL``, so targets configured per the
    standard OTel env-var docs still post protobuf.
    """
    if request.method != "POST":
        return JsonResponse({"error": {"code": "method_not_allowed", "message": "Use POST."}}, status=405)

    cred = otlp.authenticate(
        request.headers.get("Authorization"), allow_unauthenticated=otlp_config.ALLOW_UNAUTHENTICATED,
    )

    if cred is None:
        return JsonResponse(
            {"error": {"code": "unauthorized", "message": "Invalid or missing OTLP credentials."}},
            status=401,
        )

    # Cheap rejects before any decode work: an oversized body, or a gzip body
    # that inflates past the cap, is never parsed.
    if len(request.body) > otlp_config.MAX_BODY_BYTES:
        return JsonResponse(
            {"error": {"code": "payload_too_large", "message": "Export body too large."}}, status=413,
        )

    try:
        from simpleaudit.tracing.otlp import parse_otlp_json

        # Parse the export (JSON or protobuf), tag each span with the
        # authenticated target so it's attributable, then normalize into the
        # shared SpanStore (the same schema the run path and the judge's
        # evidence selection use).
        body = request.body
        if request.headers.get("Content-Encoding", "").lower() == "gzip":
            body = _bounded_gunzip(body, otlp_config.MAX_BODY_BYTES)
            if body is None:
                return JsonResponse(
                    {"error": {"code": "payload_too_large", "message": "Export body too large."}},
                    status=413,
                )
        payload = _otlp_body_to_dict(body, request.headers.get("Content-Type"))
        _normalize_span_status(payload)
        raw_spans = parse_otlp_json(payload)
        received_count = len(raw_spans)
        # Store only the spans the audit reads (tool, LLM, retrieval, ...);
        # transport and framework spans are most of an export and are dropped here.
        raw_spans = select_auditable_spans(raw_spans)
        # A span without ids can never be claimed by a run: drop it before storage.
        raw_spans = [
            s for s in raw_spans
            if s.get("span_id") and s.get("trace_id") and s["trace_id"].strip("0")
        ]
        note_export(cred.target_id, received_count, len(raw_spans))
        if not buffer_has_room(len(raw_spans)):
            # Nothing from this export is kept: the exporter retries the whole batch.
            return JsonResponse(
                {"error": {"code": "busy", "message": "Span buffer full; retry later."}},
                status=429,
                headers={"Retry-After": "1"},
            )
        for span in raw_spans:
            attrs = span.get("attributes")
            if attrs is None:
                span["attributes"] = attrs = {}
            attrs.setdefault("simpleaudit.target_id", cred.target_id)
        from audits.agentic.privacy import apply_content_capture_level

        captured_spans = apply_content_capture_level(raw_spans, cred.capture_level)
        # Classify tool/retrieval/embedding spans by their gen_ai operation so
        # both the in-memory store and DB persistence agree on the kind.
        _stamp_refined_kind(captured_spans)
        _store_for_target(cred.target_id).add_many(captured_spans)
        _trim_store(cred.target_id)
        try:
            _persist_spans(cred.target_id, captured_spans, cred.capture_level)
            otlp.maybe_prune_spans()
        except Exception:
            import logging

            logging.getLogger(__name__).exception("OTLP span persistence failed")
        return JsonResponse(
            {"partialSuccess": {"rejectedSpans": 0}, "authenticated": True}, status=200
        )
    except Exception:  # noqa: BLE001 - never fail the export; ack a rejection
        return JsonResponse({"partialSuccess": {"rejectedSpans": 1}}, status=200)


def round_trip_test(cred: OTLPCredential, user=None) -> dict:
    """Send one synthetic LLM span to this receiver with ``cred`` and check it lands.

    Exercises the same path an app uses (auth, decode, audit filter, capture
    policy), in-process, so a pass means Studio's side is healthy regardless of
    the app. The test span is removed again. Never raises.
    """
    import os

    from django.test import Client

    headers: dict[str, str] = {}
    if cred.auth_mode != OTLPCredential.AuthMode.NONE:
        secret = otlp.reveal_secret(cred, user=user)
        if secret is None:
            return {"ok": False, "stage": "secret", "detail": "No readable secret stored for this credential. Rotate the secret, then test again."}
        if cred.auth_mode == OTLPCredential.AuthMode.BASIC:
            token = base64.b64encode(f"{cred.username}:{secret}".encode()).decode()
            headers["HTTP_AUTHORIZATION"] = f"Basic {token}"
        else:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {secret}"
    trace_id, span_id = os.urandom(16).hex(), os.urandom(8).hex()
    now = time.time_ns()
    body = {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": trace_id, "spanId": span_id, "name": "studio.otlp.test",
        "startTimeUnixNano": str(now - 1_000_000), "endTimeUnixNano": str(now),
        "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}],
    }]}]}]}
    prior = last_export(cred.target_id)
    before = (prior or {}).get("kept", 0)
    started = time.monotonic()
    try:
        resp = Client(SERVER_NAME="localhost").post(
            "/otlp/v1/traces", data=json.dumps(body), content_type="application/json", **headers,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "send", "detail": f"Receiver error: {exc}"}
    ms = round((time.monotonic() - started) * 1000)
    kept_now = (last_export(cred.target_id) or {}).get("kept", 0)
    # The test is not the app's traffic: put the receipt log back as it was.
    if prior is None:
        _LAST_EXPORT.pop(cred.target_id, None)
    else:
        _LAST_EXPORT[cred.target_id] = prior
    if resp.status_code == 401:
        return {"ok": False, "stage": "auth", "detail": "Receiver rejected this credential's own stored secret (401). Rotate the secret."}
    if resp.status_code != 200:
        return {"ok": False, "stage": "receive", "detail": f"Receiver answered HTTP {resp.status_code}."}
    if kept_now <= before:
        return {"ok": False, "stage": "filter", "detail": "Receiver accepted the export but kept no span. Check the capture policy."}
    try:
        flush_pending(cred.target_id)
        OtlpSpan.objects.filter(target_id=cred.target_id, trace_id=trace_id).delete()
    except Exception:
        logging.getLogger(__name__).info("OTLP test span cleanup skipped", exc_info=True)
    return {"ok": True, "ms": ms}


def _trace_spans_seen(target_id: str, trace_id: str) -> int:
    """Spans of one trace received so far. Read-only: unlike a run's fetch it does not claim them."""
    flush_pending(target_id)
    return OtlpSpan.objects.filter(target_id=target_id, trace_id=trace_id).count() or len(
        get_spans_for_trace(target_id, trace_id)
    )


def chat_round_trip(cred: OTLPCredential, send, *, wait: float = 15.0) -> dict:
    """Call the target for real, then wait for its trace to reach the receiver.

    ``send(traceparent)`` makes one small chat request carrying that W3C header
    and returns the reply text. A target that adopts the header exports spans
    under our trace id ("linked"); one that does not still shows up as a fresh
    export after the call ("arrived"). Never raises.
    """
    import os

    trace_id = os.urandom(16).hex()
    traceparent = f"00-{trace_id}-{os.urandom(8).hex()}-01"
    t0 = time.time()
    started = time.monotonic()
    try:
        reply = send(traceparent)
    except Exception as exc:  # noqa: BLE001 - shown to the user
        return {"ok": False, "stage": "call", "detail": f"The call to the target failed: {str(exc)[:240]}"}
    reply_ms = round((time.monotonic() - started) * 1000)
    out = {"reply_ms": reply_ms, "reply": (reply or "")[:80]}
    deadline = time.monotonic() + wait
    spans, arrived = 0, False
    while True:
        spans = _trace_spans_seen(cred.target_id, trace_id)
        arrived = bool((last_export(cred.target_id) or {}).get("at", 0) > t0)
        if spans or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    try:
        OtlpSpan.objects.filter(target_id=cred.target_id, trace_id=trace_id).delete()
    except Exception:
        logging.getLogger(__name__).info("OTLP round-trip cleanup skipped", exc_info=True)
    if spans:
        return {"ok": True, "stage": "linked", "spans": spans, **out}
    if arrived:
        return {"ok": True, "stage": "arrived", **out}
    return {"ok": False, "stage": "no_trace", **out}


# ─── Credential management (session-authenticated) ──────────────────────────


def _active_project(request):
    project = getattr(request, "project", None)
    if project is None:
        raise StableAPIError(detail="No active workspace.", code="no_workspace", http_status=400)
    return project


def _require_admin(request, project) -> None:
    """Workspace ADMIN of an unarchived workspace (every caller changes credentials)."""
    from accounts.services import _require_workspace_admin, require_project_writable

    _require_workspace_admin(request.user, project)
    require_project_writable(request.user, project)


def _request_origin(request) -> str:
    """The browser-supplied ``origin`` field, or "" when absent or not a string."""
    data = request.data if isinstance(request.data, dict) else {}
    origin = data.get("origin")
    return origin.strip() if isinstance(origin, str) else ""


def _endpoint_url(request, origin: str | None = None) -> str:
    """The absolute OTLP traces URL a target should export to.

    Prefers the browser's own ``origin`` (sent by the UI) so the URL reflects
    what the user actually sees — the real domain, not a reverse-proxy or
    127.0.0.1 artifact. Falls back to the request's Host header.
    """
    origin = (origin or "").strip()
    if origin.startswith(("http://", "https://")):
        return f"{origin.rstrip('/')}/otlp/v1/traces"
    host = request.headers.get("Host") or request.get_host()
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{host}/otlp/v1/traces"


def _serialize_cred(cred: OTLPCredential, request) -> dict:
    return {
        "id": cred.id,
        "auth_mode": cred.auth_mode,
        "username": cred.username,
        "target_id": cred.target_id,
        "display_name": cred.display_name,
        "enabled": cred.enabled,
        "capture_level": cred.capture_level,
        "created_at": cred.created_at.isoformat() if cred.created_at else None,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_credentials(request):
    """List this workspace's OTLP credentials (no secrets)."""
    project = _active_project(request)
    creds = OTLPCredential.objects.filter(project=project).select_related("connection")
    return Response({"credentials": [_serialize_cred(c, request) for c in creds]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_credential(request):
    """Create a credential for a connection. Returns the one-time secret.

    Body: ``{"connection_id": int, "auth_mode": "basic"|"bearer"}``.
    The response includes the plaintext password/token exactly once.
    """
    project = _active_project(request)
    _require_admin(request, project)
    data = request.data if isinstance(request.data, dict) else {}
    conn_id = data.get("connection_id")
    auth_mode = data.get("auth_mode") or "basic"
    if not isinstance(auth_mode, str):
        raise StableAPIError(detail="auth_mode must be 'basic' or 'bearer'.", code="invalid_auth_mode")
    auth_mode = auth_mode.strip().lower()
    if not conn_id or not str(conn_id).isdecimal():
        raise StableAPIError(detail="connection_id is required.", code="invalid_connection_id")
    conn = ModelConnection.objects.filter(pk=int(conn_id), project=project).first()
    if conn is None:
        raise StableAPIError(detail="Connection not found in this workspace.", code="conn_not_found", http_status=404)

    try:
        new_cred = otlp.create_credential(
            project=project, connection=conn, auth_mode=auth_mode, user=request.user,
            capture_level=data.get("capture_level", "structural"),
        )
    except ValueError as exc:
        raise StableAPIError(detail=str(exc), code="invalid_auth_mode") from exc
    endpoint = _endpoint_url(request, origin=_request_origin(request))
    payload = {
        "credential": _serialize_cred(new_cred.credential, request),
        "secret": new_cred.secret,
        "endpoint": endpoint,
    }
    if new_cred.credential.auth_mode == OTLPCredential.AuthMode.BASIC:
        payload["env_vars"] = dict(
            otlp.otlp_env_vars(endpoint=endpoint, username=new_cred.credential.username, password=new_cred.secret)
        )
    elif new_cred.credential.auth_mode == OTLPCredential.AuthMode.BEARER:
        payload["env_vars"] = dict(otlp.otlp_bearer_env_vars(endpoint=endpoint, token=new_cred.secret))
    else:  # none — no credentials to paste; just the endpoint
        payload["env_vars"] = dict(
            otlp.otlp_open_env_vars(endpoint=endpoint)
        )
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def rotate_credential(request, cred_id):
    """Re-issue a credential's secret. The old secret stops working.

    Returns the new one-time secret plus the env vars to paste into the target.
    """
    project = _active_project(request)
    _require_admin(request, project)
    cred = OTLPCredential.objects.filter(pk=cred_id, project=project).first()
    if cred is None:
        raise StableAPIError(detail="Credential not found.", code="cred_not_found", http_status=404)
    try:
        new_cred = otlp.rotate_credential(cred)
    except ValueError as e:
        raise StableAPIError(detail=str(e), code="cannot_rotate", http_status=400)
    endpoint = _endpoint_url(request, origin=_request_origin(request))
    payload = {
        "credential": _serialize_cred(new_cred.credential, request),
        "secret": new_cred.secret,
        "endpoint": endpoint,
    }
    if new_cred.credential.auth_mode == OTLPCredential.AuthMode.BASIC:
        payload["env_vars"] = dict(
            otlp.otlp_env_vars(endpoint=endpoint, username=new_cred.credential.username, password=new_cred.secret)
        )
    else:  # bearer
        payload["env_vars"] = dict(otlp.otlp_bearer_env_vars(endpoint=endpoint, token=new_cred.secret))
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def revoke_credential(request, cred_id):
    """Revoke (disable) a credential so it can no longer push traces."""
    project = _active_project(request)
    _require_admin(request, project)
    cred = OTLPCredential.objects.filter(pk=cred_id, project=project).first()
    if cred is None:
        raise StableAPIError(detail="Credential not found.", code="cred_not_found", http_status=404)
    cred.enabled = False
    cred.save(update_fields=["enabled", "updated_at"])
    clear_target_spans(cred.target_id)
    return Response({"ok": True, "credential": _serialize_cred(cred, request)})
