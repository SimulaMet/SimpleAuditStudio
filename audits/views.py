import json
import logging
import time

from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audits.events import ScenarioResult, list_events
from audits.models import AuditRun
from audits.serializers import AuditRunCreateSerializer, AuditRunSerializer
from audits.services import create_audit_run, submit_audit_run
from infra.exceptions import StableAPIError
from infra.middleware import set_correlation_context
from model_registry.models import ModelEndpoint
from accounts.models import Project
from scenarios.models import ScenarioSetVersion
from accounts.services import ensure_project_access

logger = logging.getLogger("simpleaudit.audit")

# Terminal event kinds: once one of these is seen for the run, the stream can end.
_TERMINAL_EVENT_KINDS = {"run_completed", "run_failed", "run_cancelled"}
_SSE_POLL_INTERVAL_SECONDS = 1.0
_SSE_MAX_DURATION_SECONDS = 3600


def _get_project_or_404(project_id) -> Project:
    try:
        return Project.objects.get(pk=project_id)
    except Project.DoesNotExist as exc:
        raise StableAPIError(detail="Project not found.", code="project_not_found", http_status=404) from exc


def _require_project_access(user, project: Project):
    if not ensure_project_access(user, project):
        raise StableAPIError(detail="Project access denied.", code="project_access_denied", http_status=403)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_audit_runs(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    runs = AuditRun.objects.filter(project=project).select_related("scenario_set_version")
    return Response(AuditRunSerializer(runs, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_audit_run(request, project_id, run_id):
    set_correlation_context(audit_run_id=run_id)
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    queryset = AuditRun.objects.filter(id=run_id, project=project).select_related(
        "scenario_set_version", "target_endpoint", "auditor_endpoint", "judge_endpoint"
    )
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist as exc:
        raise StableAPIError(detail="Audit run not found.", code="audit_run_not_found", http_status=404) from exc
    return Response(AuditRunSerializer(run).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_audit_run_results(request, project_id, run_id):
    """Per-scenario results for a run, joined with the pinned version item.

    Returns one row per scenario: the durable ``ScenarioResult`` (status,
    attempts, full structured result) plus the scenario identity from the frozen
    ``ScenarioSetVersionItem`` so the UI can label rows without re-deriving them.
    """
    set_correlation_context(audit_run_id=run_id)
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    queryset = AuditRun.objects.filter(id=run_id, project=project)
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist as exc:
        raise StableAPIError(detail="Audit run not found.", code="audit_run_not_found", http_status=404) from exc

    items = {
        str(vi.id): vi
        for vi in run.scenario_set_version.items.select_related("scenario", "revision")
    }
    results = ScenarioResult.objects.filter(run_id=run.id).order_by("version_item_id")
    data = []
    for r in results:
        item = items.get(str(r.version_item_id))
        data.append(
            {
                "version_item_id": r.version_item_id,
                "status": r.status,
                "attempts": r.attempts,
                "result": r.result or {},
                "scenario_key": item.scenario.key if item and item.scenario else None,
                "scenario_title": item.scenario.title if item and item.scenario else None,
                "position": item.position if item else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
        )
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_audit_run_view(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    serializer = AuditRunCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        scenario_set_version = ScenarioSetVersion.objects.get(id=data["scenario_set_version_id"], scenario_set__project=project)
        target_endpoint = ModelEndpoint.objects.get(id=data["target_endpoint_id"], project=project)
        auditor_endpoint = ModelEndpoint.objects.get(id=data["auditor_endpoint_id"], project=project)
        judge_endpoint = ModelEndpoint.objects.get(id=data["judge_endpoint_id"], project=project)
    except (ScenarioSetVersion.DoesNotExist, ModelEndpoint.DoesNotExist) as exc:
        raise StableAPIError(detail="Audit input not found in project.", code="audit_input_not_found", http_status=404) from exc

    run = create_audit_run(
        project=project,
        user=request.user,
        name=data["name"],
        scenario_set_version=scenario_set_version,
        target_endpoint=target_endpoint,
        auditor_endpoint=auditor_endpoint,
        judge_endpoint=judge_endpoint,
    )

    # Enqueue durable work. This is best-effort: if the job system is unavailable
    # the run stays queued and records why (see submit_audit_run), so a frozen
    # experiment record is never lost to a transient infrastructure failure.
    submit_audit_run(run)
    run.refresh_from_db()
    return Response(AuditRunSerializer(run).data, status=status.HTTP_201_CREATED)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_audit_run(request, project_id, run_id):
    """Request cancellation of an active audit run.

    Sets the durable CANCELLED flag on the run row. The worker observes this
    flag between scenario executions and skips remaining scenarios. Already-
    completed scenarios are unaffected. Idempotent: cancelling a terminal run
    is a no-op.
    """
    set_correlation_context(audit_run_id=run_id)
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    queryset = AuditRun.objects.filter(id=run_id, project=project)
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist as exc:
        raise StableAPIError(detail="Audit run not found.", code="audit_run_not_found", http_status=404) from exc

    if run.status in (AuditRun.Status.COMPLETED, AuditRun.Status.FAILED, AuditRun.Status.CANCELLED):
        raise StableAPIError(
            detail=f"Run is already {run.status}; cannot cancel.",
            code="run_already_terminal",
            http_status=409,
        )

    run.status = AuditRun.Status.CANCELLED
    if run.finished_at is None:
        run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at"])
    logger.info("Audit run %s cancellation requested by user %s", run.id, request.user.username)
    return Response(AuditRunSerializer(run).data)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def poll_audit_run_events(request, project_id, run_id):
    """One-shot JSON snapshot of durable events with id > ``after_id``.

    This is the polling counterpart to :func:`stream_audit_run_events`. It exists
    because token-authenticated clients (the SPA) cannot send an ``Authorization``
    header through ``EventSource``. Each call returns only new events since the
    client's cursor, so it is reconnect-safe and cheap. The client stops polling
    once it observes a terminal run event.
    """
    set_correlation_context(audit_run_id=run_id)
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    queryset = AuditRun.objects.filter(id=run_id, project=project)
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist as exc:
        raise StableAPIError(detail="Audit run not found.", code="audit_run_not_found", http_status=404) from exc

    after_id_raw = request.query_params.get("after_id") or "0"
    try:
        after_id = int(after_id_raw)
    except (TypeError, ValueError):
        after_id = 0

    return Response(list_events(run.id, after_id=after_id))


def stream_audit_run_events(request, project_id, run_id):
    """Server-Sent Events stream of durable progress for an audit run.

    Plain Django view (not DRF) to avoid content-negotiation issues with
    text/event-stream responses. Auth is enforced via session login.

    Progress is durable (Postgres ``AuditEvent`` rows), so a browser reconnect or
    server restart does not lose it. The client passes its last received event id
    via the ``Last-Event-ID`` header (or ``?after_id=``) and receives only events
    with a higher id — standard SSE replay semantics. The stream ends when a
    terminal run event (completed/failed/cancelled) is observed.
    """
    from django.http import Http404

    # This is called via URL dispatch; enforce auth manually since we're not using DRF.
    if not request.user.is_authenticated:
        from django.http import HttpResponse
        return HttpResponse(status=401)

    set_correlation_context(audit_run_id=run_id)
    project = _get_project_or_404(project_id)
    if not ensure_project_access(request.user, project):
        from django.http import HttpResponse
        return HttpResponse(status=403)
    queryset = AuditRun.objects.filter(id=run_id, project=project)
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist:
        raise Http404("Audit run not found.")

    after_id_raw = request.headers.get("Last-Event-ID") or request.GET.get("after_id") or "0"
    try:
        after_id = int(after_id_raw)
    except (TypeError, ValueError):
        after_id = 0

    def event_stream():
        sent_after = after_id
        start = time.monotonic()
        while True:
            if time.monotonic() - start > _SSE_MAX_DURATION_SECONDS:
                yield "event: timeout\ndata: {}\n\n"
                break
            events = list_events(run.id, after_id=sent_after)
            for e in events:
                sent_after = e["id"]
                data = json.dumps({"kind": e["kind"], "version_item_id": e["version_item_id"], "payload": e["payload"]})
                yield f"id: {e['id']}\nevent: {e['kind']}\ndata: {data}\n\n"
                if e["kind"] in _TERMINAL_EVENT_KINDS:
                    return
            time.sleep(_SSE_POLL_INTERVAL_SECONDS)

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def compare_audit_runs(request, project_id):
    """Compare multiple audit runs by ID (comma-separated ``?run_ids=1,2,3``).

    Returns intersection-based results with compatibility warnings. Does not
    silently compare incompatible experiments — warnings are always surfaced.
    """
    from audits.comparison import ComparisonIncompatible, compare_runs

    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)

    raw_ids = request.query_params.get("run_ids", "")
    try:
        run_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]
    except ValueError:
        raise StableAPIError(detail="run_ids must be comma-separated integers.", code="bad_request", http_status=400)

    if len(run_ids) < 2:
        raise StableAPIError(detail="Provide at least 2 run IDs to compare.", code="bad_request", http_status=400)
    if len(run_ids) > 10:
        raise StableAPIError(detail="Compare at most 10 runs at a time.", code="bad_request", http_status=400)

    try:
        result = compare_runs(project, run_ids)
    except ComparisonIncompatible as exc:
        raise StableAPIError(detail="; ".join(exc.reasons), code="incompatible_runs", http_status=409) from exc

    return Response(result)
