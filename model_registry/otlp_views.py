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

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from simpleaudit.tracing.auth import parse_basic_header, parse_bearer_header
from simpleaudit.tracing.store import SpanStore, normalize_span

from infra.exceptions import StableAPIError
from model_registry import otlp_services as otlp
from model_registry.models import ModelConnection, OTLPCredential, OtlpSpan

# In-memory span store for the shared receiver, keyed by target_id. Each bucket
# is an engine ``SpanStore`` (the same normalized schema the run path and the
# judge's evidence selection use), so spans ingested here are directly
# consumable by ``select_spans``. (A persistent backend can replace this
# without changing the endpoint contract.)
_SPAN_STORE: dict[str, SpanStore] = {}


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
    """
    if not trace_id:
        return []
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


def _persist_spans(target_id: str, raw_spans: list[dict]) -> None:
    """Idempotently persist normalized spans to the DB for cross-process reads.

    A persistence failure must never fail the OTLP export (the target's exporter
    would retry and flood); we log and keep the in-memory copy authoritative.
    """
    for raw in raw_spans:
        span = normalize_span(raw)
        span_id = span.get("span_id") or ""
        if not span_id:
            continue
        OtlpSpan.objects.update_or_create(
            target_id=target_id,
            trace_id=span.get("trace_id") or "",
            span_id=span_id,
            defaults={
                "name": (span.get("name") or "span")[:500],
                "kind": (span.get("kind") or "CHAIN")[:50],
                "parent_span_id": span.get("parent_span_id"),
                "start_time": span.get("start_time"),
                "end_time": span.get("end_time"),
                "status": (span.get("status") or "OK")[:20],
                "attributes": span.get("attributes") or {},
            },
        )


def clear_target_spans(target_id: str) -> None:
    _SPAN_STORE.pop(target_id, None)


# ─── Ingestion (machine-to-machine) ─────────────────────────────────────────


@csrf_exempt
def otlp_traces(request):
    """Accept an OTLP/HTTP-JSON trace export from an authenticated target.

    Auth: ``Authorization: Basic base64(user:pass)`` or ``Bearer <token>``.
    Returns the OTLP ack (200) on success, 401 on auth failure, 405 on a
    non-POST. Spans are tagged with the credential's ``target_id``.
    """
    if request.method != "POST":
        return JsonResponse({"error": {"code": "method_not_allowed", "message": "Use POST."}}, status=405)

    authorization = request.headers.get("Authorization")
    cred = None
    if authorization and authorization.strip().lower().startswith("basic"):
        parsed = parse_basic_header(authorization)
        if parsed:
            cred = otlp.verify_basic(*parsed)
    elif authorization and authorization.strip().lower().startswith("bearer"):
        token = parse_bearer_header(authorization)
        if token:
            cred = otlp.verify_bearer(token)

    # No Authorization header (or an unrecognized scheme): fall back to an
    # enabled "none" credential for this workspace, if any. This is the
    # unauthenticated path for targets that can't send credentials.
    if cred is None:
        cred = OTLPCredential.objects.filter(
            auth_mode=OTLPCredential.AuthMode.NONE, enabled=True
        ).first()

    if cred is None:
        return JsonResponse(
            {"error": {"code": "unauthorized", "message": "Invalid or missing OTLP credentials."}},
            status=401,
        )

    try:
        from simpleaudit.tracing.otlp import parse_otlp_json

        # Parse the export, tag each span with the authenticated target so it's
        # attributable, then normalize into the shared SpanStore (the same
        # schema the run path and the judge's evidence selection use).
        raw_spans = parse_otlp_json(request.body)
        for span in raw_spans:
            attrs = span.get("attributes")
            if attrs is None:
                span["attributes"] = attrs = {}
            attrs.setdefault("simpleaudit.target_id", cred.target_id)
        _store_for_target(cred.target_id).add_many(raw_spans)
        try:
            _persist_spans(cred.target_id, raw_spans)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("OTLP span persistence failed")
        return JsonResponse(
            {"partialSuccess": {"rejectedSpans": 0}, "authenticated": True}, status=200
        )
    except Exception:  # noqa: BLE001 - never fail the export; ack a rejection
        return JsonResponse({"partialSuccess": {"rejectedSpans": 1}}, status=200)


# ─── Credential management (session-authenticated) ──────────────────────────


def _active_project(request):
    project = getattr(request, "project", None)
    if project is None:
        raise StableAPIError(detail="No active workspace.", code="no_workspace", http_status=400)
    return project


def _require_admin(request, project) -> None:
    from accounts.services import _require_workspace_admin

    _require_workspace_admin(request.user, project)


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
    data = request.data or {}
    conn_id = data.get("connection_id")
    auth_mode = (data.get("auth_mode") or "basic").strip().lower()
    if not conn_id or not str(conn_id).isdigit():
        raise StableAPIError(detail="connection_id is required.", code="invalid_connection_id")
    conn = ModelConnection.objects.filter(pk=int(conn_id), project=project).first()
    if conn is None:
        raise StableAPIError(detail="Connection not found in this workspace.", code="conn_not_found", http_status=404)

    new_cred = otlp.create_credential(project=project, connection=conn, auth_mode=auth_mode, user=request.user)
    endpoint = _endpoint_url(request, origin=(request.data.get("origin") or "").strip())
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
    endpoint = _endpoint_url(request, origin=(request.data.get("origin") or "").strip())
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
    otlp.clear_target_spans(cred.target_id)
    return Response({"ok": True, "credential": _serialize_cred(cred, request)})
