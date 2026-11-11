import json
import logging
import os
import threading
import time

from django.db import close_old_connections
from django.http import StreamingHttpResponse
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Project
from accounts.services import ensure_project_access, require_project_writable
from audits.events import ScenarioResult, list_events
from audits.models import AuditRun
from audits.serializers import (
    AuditRunCreateSerializer,
    AuditRunListSerializer,
    AuditRunSerializer,
)
from audits.services import (
    build_simpleaudit_export,
    cancel_run,
    create_audit_run,
    submit_audit_run,
)
from infra.exceptions import StableAPIError
from infra.middleware import set_correlation_context
from infra.ui import is_id
from judges.models import Judge, JudgeVersion
from judges.services import default_judge_version
from model_registry.models import Agent, RegisteredModel
from scenarios.models import ScenarioSetVersion

logger = logging.getLogger("simpleaudit.audit")

# Terminal event kinds: once one of these is seen for the run, the stream can end.
_TERMINAL_EVENT_KINDS = {"run_completed", "run_failed", "run_cancelled"}
_SSE_POLL_INTERVAL_SECONDS = 1.0
# While a run is quiet the poll backs off (1s -> 5s) so idle viewers cost less;
# the first event resets it. A comment line every 15s keeps proxies from closing
# an idle stream.
_SSE_MAX_POLL_INTERVAL_SECONDS = 5.0
_SSE_HEARTBEAT_SECONDS = 15.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Each open stream holds one web thread for up to an hour. Caps are per web
# process: one user's tabs cannot take more than a few, and streams as a whole
# never take more than half the process's threads, so pages and /chat/authz
# always have threads left. Over the cap the stream answers 429 and the
# browser's EventSource retries with backoff.
_SSE_MAX_STREAMS_PER_USER = _env_int("SIMPLEAUDIT_SSE_MAX_STREAMS_PER_USER", 3)
_SSE_MAX_STREAMS = _env_int(
    "SIMPLEAUDIT_SSE_MAX_STREAMS", max(1, _env_int("GUNICORN_THREADS", 16) // 2)
)
_sse_lock = threading.Lock()
_sse_open: dict[int, int] = {}


def _sse_acquire(user_id: int) -> bool:
    with _sse_lock:
        if sum(_sse_open.values()) >= _SSE_MAX_STREAMS:
            return False
        if _sse_open.get(user_id, 0) >= _SSE_MAX_STREAMS_PER_USER:
            return False
        _sse_open[user_id] = _sse_open.get(user_id, 0) + 1
        return True


def _sse_release(user_id: int) -> None:
    with _sse_lock:
        left = _sse_open.get(user_id, 0) - 1
        if left > 0:
            _sse_open[user_id] = left
        else:
            _sse_open.pop(user_id, None)


class _SlotStream:
    """Iterator around the event generator that frees the stream slot on close.

    Django calls ``close()`` on the streaming content when the response ends,
    even if the generator never started (its own ``finally`` would not run then).
    """

    def __init__(self, gen, user_id: int):
        self._gen = gen
        self._user_id = user_id
        self._released = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def close(self):
        try:
            self._gen.close()
        finally:
            if not self._released:
                self._released = True
                _sse_release(self._user_id)
_SSE_MAX_DURATION_SECONDS = 3600
# Max events read per poll; a backlog drains over successive polls.
_SSE_BATCH_SIZE = 1000


class _RunListPagination(LimitOffsetPagination):
    """``?limit=&offset=``; response is ``{count, next, previous, results}``."""

    default_limit = 50
    max_limit = 200


class _ResultListPagination(LimitOffsetPagination):
    # Each row carries a full transcript, so pages stay small.
    default_limit = 100
    max_limit = 500


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
    """Runs of a project, newest first, paginated.

    Rows are slim (no config snapshots); ``?expand=snapshots`` returns the full
    detail shape for each row instead.
    """
    runs = AuditRun.objects.filter(project=project).order_by("-created_at", "-id")
    paginator = _RunListPagination()
    if request.query_params.get("expand") == "snapshots":
        page = paginator.paginate_queryset(runs.select_related("scenario_set_version"), request)
        return paginator.get_paginated_response(AuditRunSerializer(page, many=True).data)
    from audits.monitors import pass_counts

    page = paginator.paginate_queryset(
        runs.select_related("scenario_set_version", "target_model", "judge_model").defer(
            # Never shown in a list row; frozen names/judge read the target and judge snapshots.
            "agent_config_snapshot", "auditor_config_snapshot", "generation_parameters_snapshot",
            "trace_config", "runtime_metadata", "summary_metrics",
        ),
        request,
    )
    counts = pass_counts([r.id for r in page])
    data = AuditRunListSerializer(page, many=True, context={"pass_counts": counts}).data
    return paginator.get_paginated_response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_audit_run(request, project_id, run_id):
    set_correlation_context(audit_run_id=run_id)
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    queryset = AuditRun.objects.filter(id=run_id, project=project).select_related(
        "scenario_set_version", "target_model", "auditor_model", "judge_model"
    )
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist as exc:
        raise StableAPIError(detail="Audit run not found.", code="audit_run_not_found", http_status=404) from exc
    return Response(AuditRunSerializer(run).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def export_audit_run(request, project_id, run_id):
    """Download a run's durable results as canonical SimpleAudit engine JSON.

    Reassembles the persisted per-scenario ``AuditResult`` payloads into the same
    shape core writes via ``results.save()``, so a single downstream converter
    (e.g. Every Eval Ever) can consume Studio runs and core CLI runs alike.
    Exports whatever durable results exist; a partially complete run exports its
    finished scenarios.
    """
    set_correlation_context(audit_run_id=run_id)
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    queryset = AuditRun.objects.filter(id=run_id, project=project).select_related(
        "scenario_set_version"
    )
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist as exc:
        raise StableAPIError(detail="Audit run not found.", code="audit_run_not_found", http_status=404) from exc

    payload = build_simpleaudit_export(run)
    stem = slugify(run.name) or f"audit-run-{run.id}"
    from infra.export_utils import buffered, json_chunks

    # The engine computes the summary over every result, so the document is
    # built whole; it is encoded and sent incrementally instead of as one string.
    response = StreamingHttpResponse(buffered(json_chunks(payload)), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{stem}.json"'
    return response


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
    paginator = _ResultListPagination()
    results = paginator.paginate_queryset(
        ScenarioResult.objects.filter(run_id=run.id).order_by("version_item_id", "id"), request
    )
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
    return paginator.get_paginated_response(data)


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
        target_model = RegisteredModel.objects.get(id=data["target_model_id"], project=project)
        auditor_model = RegisteredModel.objects.get(id=data["auditor_model_id"], project=project)
        judge_model = RegisteredModel.objects.get(id=data["judge_model_id"], project=project)
        agent = None
        if data.get("agent_id"):
            agent = Agent.objects.get(id=data["agent_id"], project=project)
        if data.get("judge_version_id"):
            judge = JudgeVersion.objects.select_related("judge").get(id=data["judge_version_id"], judge__project=project)
        elif data.get("judge_id"):
            judge = Judge.objects.get(id=data["judge_id"], project=project).latest
            if judge is None:
                raise JudgeVersion.DoesNotExist
        else:
            judge = default_judge_version(project, request.user)
    except (ScenarioSetVersion.DoesNotExist, RegisteredModel.DoesNotExist, Judge.DoesNotExist,
            JudgeVersion.DoesNotExist, Agent.DoesNotExist) as exc:
        raise StableAPIError(detail="Audit input not found in project.", code="audit_input_not_found", http_status=404) from exc

    run = create_audit_run(
        project=project,
        user=request.user,
        name=data["name"],
        scenario_set_version=scenario_set_version,
        target_model=None if agent else target_model,
        auditor_model=auditor_model,
        judge_model=judge_model,
        judge=judge,
        trace_config=data.get("trace_config") or None,
        agent=agent,
        max_turns_override=data.get("max_turns"),
        n_repetitions_override=data.get("n_repetitions"),
        language_override=data.get("language") or None,
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
    require_project_writable(request.user, project)
    queryset = AuditRun.objects.filter(id=run_id, project=project)
    try:
        run = queryset.get()
    except AuditRun.DoesNotExist as exc:
        raise StableAPIError(detail="Audit run not found.", code="audit_run_not_found", http_status=404) from exc

    if not cancel_run(run, request.user):
        raise StableAPIError(
            detail=f"Run is already {run.status}; cannot cancel.",
            code="run_already_terminal",
            http_status=409,
        )
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
    # Out-of-range ids (SQLite integers are 64-bit) count as "from the start".
    after_id = int(after_id_raw) if is_id(str(after_id_raw)) else 0

    # Capped like the SSE batch: a client resuming from 0 on a big run pages
    # forward with the last id it received instead of pulling the whole log.
    return Response(list_events(run.id, after_id=after_id, limit=_SSE_BATCH_SIZE))


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
    # Out-of-range ids (SQLite integers are 64-bit) count as "from the start".
    after_id = int(after_id_raw) if is_id(str(after_id_raw)) else 0

    def event_stream():
        sent_after = after_id
        start = time.monotonic()
        interval = _SSE_POLL_INTERVAL_SECONDS
        last_write = start
        terminal_idle = 0
        try:
            while True:
                if time.monotonic() - start > _SSE_MAX_DURATION_SECONDS:
                    yield "event: timeout\ndata: {}\n\n"
                    break
                events = list_events(run.id, after_id=sent_after, limit=_SSE_BATCH_SIZE)
                if events:
                    interval = _SSE_POLL_INTERVAL_SECONDS
                for e in events:
                    last_write = time.monotonic()
                    sent_after = e["id"]
                    data = json.dumps({"kind": e["kind"], "version_item_id": e["version_item_id"], "payload": e["payload"], "ts": e["created_at"]})
                    yield f"id: {e['id']}\nevent: {e['kind']}\ndata: {data}\n\n"
                    if e["kind"] in _TERMINAL_EVENT_KINDS:
                        return
                if not events:
                    # A run can be finished while its terminal event is missing
                    # (pruned, or written by an older version): without this the
                    # stream would idle for the full hour holding a thread. Two
                    # quiet polls in a row let a just-written terminal event land first.
                    status = AuditRun.objects.filter(pk=run.id).values_list("status", flat=True).first()
                    terminal_idle = terminal_idle + 1 if status in AuditRun.TERMINAL_STATUSES else 0
                    if terminal_idle >= 2:
                        kind = f"run_{status}"
                        data = json.dumps({"kind": kind, "version_item_id": "_run", "payload": {"synthetic": True}, "ts": None})
                        yield f"event: {kind}\ndata: {data}\n\n"
                        return
                if len(events) < _SSE_BATCH_SIZE:
                    # Release (or recycle) the DB connection while idle so a
                    # long-lived stream does not pin a Postgres connection per thread.
                    close_old_connections()
                    if time.monotonic() - last_write >= _SSE_HEARTBEAT_SECONDS:
                        last_write = time.monotonic()
                        yield ": keepalive\n\n"
                    time.sleep(interval)
                    if not events:
                        interval = min(interval * 1.5, _SSE_MAX_POLL_INTERVAL_SECONDS)
        except (GeneratorExit, SystemExit, KeyboardInterrupt):
            # Normal stream teardown: the client disconnected (GeneratorExit is
            # raised when the response iterator is closed), or the worker process
            # is shutting down / interrupted (SystemExit / Ctrl+C). These are
            # expected during an hour-long poll loop — swallow them so they don't
            # surface as noise in error tracking. Re-raising lets Python run any
            # pending finally blocks and propagate the correct close semantics.
            logger.debug("SSE stream for run %s ended early (client disconnect or shutdown)", run.id)
            raise

    if not _sse_acquire(request.user.pk):
        from django.http import HttpResponse

        busy = HttpResponse("Too many live streams; retrying shortly.", status=429, content_type="text/plain")
        busy["Retry-After"] = "15"
        return busy
    response = StreamingHttpResponse(_SlotStream(event_stream(), request.user.pk), content_type="text/event-stream")
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
    parts = [x.strip() for x in raw_ids.split(",") if x.strip()]
    if not all(is_id(x) for x in parts):
        raise StableAPIError(detail="run_ids must be comma-separated integers.", code="bad_request", http_status=400)
    run_ids = [int(x) for x in parts]

    if len(run_ids) < 2:
        raise StableAPIError(detail="Provide at least 2 run IDs to compare.", code="bad_request", http_status=400)
    if len(run_ids) > 10:
        raise StableAPIError(detail="Compare at most 10 runs at a time.", code="bad_request", http_status=400)

    try:
        result = compare_runs(project, run_ids)
    except ComparisonIncompatible as exc:
        raise StableAPIError(detail="; ".join(exc.reasons), code="incompatible_runs", http_status=409) from exc

    return Response(result)
