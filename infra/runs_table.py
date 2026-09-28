"""Dashboard runs table: filtering, sorting and row data for the interactive grid.

The dashboard page renders an empty Tabulator grid that loads rows from
``RunsDataView`` (server-side paging, sorting and filtering, so it stays fast
with many runs). The same ``filtered_runs`` feeds the CSV export, so an export
always matches what is on screen. Column layout (order, width, visibility) is
saved per user through ``PreferenceView``.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views import View

from audits.models import AuditRun
from audits.services import frozen_judge
from infra.ui import ProjectMixin, write_block_reason

PAGE_SIZES = (10, 25, 50, 100)

# Remote sort: Tabulator field -> ORM ordering. Computed fields are not sortable.
SORT_FIELDS = {
    "id": "id",
    "name": "name",
    "status": "status",
    "created_at": "created_at",
    "target": "target_model__display_name",
    "auditor": "auditor_model__display_name",
    "judge": "judge_version__judge__name",
    "scenario_set": "scenario_set_version__scenario_set__name",
    "created_by": "created_by__username",
}

# Preference keys the UI may store (value size is capped).
PREFERENCE_KEYS = {"dashboard_columns"}
MAX_PREFERENCE_BYTES = 20_000


def _date(raw: str, *, end: bool = False):
    try:
        d = datetime.fromisoformat(raw).date() if raw and len(raw) == 10 else None
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    moment = timezone.make_aware(datetime.combine(d, time.min))
    return moment + timedelta(days=1) if end else moment


def filtered_runs(project, params):
    """Runs of ``project`` matching the dashboard filters in ``params`` (a QueryDict)."""
    qs = AuditRun.objects.filter(project=project).select_related(
        "scenario_set_version__scenario_set", "target_model", "auditor_model", "judge_model",
        "judge_version__judge", "experiment", "monitor", "created_by",
    )
    status = params.get("status", "")
    if status == "archived":
        qs = qs.filter(archived=True)
    else:
        qs = qs.filter(archived=False)
        if status == "active":
            qs = qs.exclude(status__in=AuditRun.TERMINAL_STATUSES)
        elif status in AuditRun.TERMINAL_STATUSES:
            qs = qs.filter(status=status)
    q = (params.get("q") or "").strip()
    if q:
        filt = (
            Q(name__icontains=q)
            | Q(scenario_set_version__scenario_set__name__icontains=q)
            | Q(target_model__display_name__icontains=q)
            | Q(target_model__model_id__icontains=q)
        )
        if q.lstrip("#").isdigit():
            filt |= Q(pk=int(q.lstrip("#")))
        qs = qs.filter(filt)
    for param, field in (("target", "target_model_id"), ("set", "scenario_set_version__scenario_set_id"),
                         ("judge", "judge_version__judge_id"),
                         ("experiment", "experiment_id"), ("monitor", "monitor_id")):
        ids = [v for v in params.getlist(param) if v.isdigit()]
        if ids:
            qs = qs.filter(**{f"{field}__in": ids})
    start, end = _date(params.get("from")), _date(params.get("to"), end=True)
    if start:
        qs = qs.filter(created_at__gte=start)
    if end:
        qs = qs.filter(created_at__lt=end)
    return qs


def _ordering(params) -> list[str]:
    field = SORT_FIELDS.get(params.get("sort", ""), "created_at")
    desc = params.get("dir", "desc") != "asc"
    return [f"-{field}" if desc else field, "-id"]


def _frozen_name(run, role: str) -> str:
    snap = getattr(run, f"{role}_config_snapshot") or {}
    return snap.get("display_name") or snap.get("model_id") or getattr(run, f"{role}_model").display_name


def run_row(run, counts: dict) -> dict:
    params = run.generation_parameters_snapshot or {}
    c = counts.get(run.id) or {"k": 0, "n": 0}
    return {
        "id": run.id,
        "name": run.name,
        "url": f"/runs/{run.id}/",
        "status": run.status,
        "status_label": run.get_status_display(),
        "active": run.is_active,
        "archived": run.archived,
        "completed": run.completed_scenarios,
        "total": run.total_scenarios,
        "reps": int(params.get("n_repetitions") or 1),
        "pass_rate": round(c["k"] * 100 / c["n"], 1) if c["n"] else None,
        "trials": c["n"],
        # Names as frozen when the run was created (models can be renamed later).
        "target": _frozen_name(run, "target"),
        "target_id": (run.target_config_snapshot or {}).get("model_id") or run.target_model.model_id,
        "auditor": _frozen_name(run, "auditor"),
        "judge": frozen_judge(run)["label"],
        "judge_model": _frozen_name(run, "judge"),
        "scenario_set": run.scenario_set_version.label,
        "max_turns": params.get("max_turns") or 5,
        "language": params.get("language") or "English",
        "duration": run.duration_display,
        "engine": run.simpleaudit_version,
        "created_by": run.created_by.username if run.created_by else "",
        "created_at": run.created_at.isoformat(),
        "experiment": {"id": run.experiment_id, "name": run.experiment.name} if run.experiment_id else None,
        "monitor": {"id": run.monitor_id, "name": run.monitor.name} if run.monitor_id else None,
        "error": run.error_message or run.error_code,
    }


class RunsDataView(ProjectMixin, View):
    """GET /runs/data/ — one page of runs for the dashboard grid (Tabulator remote format)."""

    def get(self, request):
        from audits.monitors import pass_counts

        params = request.GET
        qs = filtered_runs(request.project, params).order_by(*_ordering(params))
        try:
            size = int(params.get("size", 25))
        except ValueError:
            size = 25
        size = size if size in PAGE_SIZES else 25
        total = qs.count()
        last_page = max(1, -(-total // size))
        try:
            page = min(max(1, int(params.get("page", 1))), last_page)
        except ValueError:
            page = 1
        runs = list(qs[(page - 1) * size: page * size])
        counts = pass_counts([r.id for r in runs])
        return JsonResponse({
            "last_page": last_page,
            "total": total,
            "data": [run_row(r, counts) for r in runs],
        })


class RunsBulkView(ProjectMixin, View):
    """POST /runs/bulk/ — {"action": "archive"|"unarchive"|"cancel", "ids": [...]}."""

    def post(self, request):
        from audits.services import cancel_run
        reason = write_block_reason(request)
        if reason:
            return JsonResponse({"error": reason}, status=403)
        try:
            body = json.loads(request.body or b"{}")
            ids = [int(i) for i in body.get("ids", [])][:500]
        except (ValueError, TypeError):
            return JsonResponse({"error": "Invalid request."}, status=400)
        action = body.get("action")
        runs = AuditRun.objects.filter(project=request.project, pk__in=ids)
        if action in ("archive", "unarchive"):
            changed = runs.update(archived=action == "archive")
        elif action == "cancel":
            changed = sum(cancel_run(run, request.user) for run in runs)
        else:
            return JsonResponse({"error": "Unknown action."}, status=400)
        return JsonResponse({"changed": changed})


class PreferenceView(ProjectMixin, View):
    """POST /me/preferences/ — {"key": ..., "value": ...}; value null clears it."""

    def post(self, request):
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"error": "Invalid JSON."}, status=400)
        key = body.get("key")
        if key not in PREFERENCE_KEYS:
            return JsonResponse({"error": "Unknown preference."}, status=400)
        value = body.get("value")
        if len(json.dumps(value)) > MAX_PREFERENCE_BYTES:
            return JsonResponse({"error": "Preference too large."}, status=400)
        prefs = dict(request.user.preferences or {})
        if value is None:
            prefs.pop(key, None)
        else:
            prefs[key] = value
        request.user.preferences = prefs
        request.user.save(update_fields=["preferences"])
        return JsonResponse({"ok": True})


class RunsExportView(ProjectMixin, View):
    """GET /runs/export.csv — the dashboard's current filters, as CSV."""

    def get(self, request):
        from audits.monitors import pass_counts

        runs = list(filtered_runs(request.project, request.GET).order_by(*_ordering(request.GET))[:10000])
        counts = pass_counts([r.id for r in runs])
        cols = ["id", "name", "status", "scenario_set", "target", "auditor", "judge", "judge_model", "completed", "total",
                "reps", "pass_rate", "max_turns", "language", "duration", "created_by", "created_at"]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for run in runs:
            row = run_row(run, counts)
            writer.writerow([row[c] if row[c] is not None else "" for c in cols])
        response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="runs.csv"'
        return response
