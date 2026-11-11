"""Durable audit progress events and idempotent per-scenario results.

This is the production counterpart of ``spike/event_store.py``. The spike used a
SQLite stand-in; here the durable source of truth is PostgreSQL:

- ``AuditEvent`` rows are append-only and carry a monotonically increasing
  ``id`` so SSE can replay from ``Last-Event-ID`` after a reconnect without
  losing progress.
- ``ScenarioResult`` rows are written idempotently keyed on
  ``(run_id, version_item_id)`` with ``attempts = MAX(...)`` so duplicate
  execution (retries, re-dispatch) cannot create duplicate final results.

PostgreSQL remains authoritative for SimpleAudit domain state. Hatchet workflow
events are inputs only; the worker bridges them into these rows.
"""
from __future__ import annotations

from django.db import models, transaction

from infra.db import retry_if_locked


class AuditEvent(models.Model):
    """Append-only durable progress event for an audit run."""

    run_id = models.PositiveBigIntegerField()
    version_item_id = models.CharField(max_length=64)
    kind = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_audit_event"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["run_id", "id"], name="audit_event_run_id_idx"),
            # Per-scenario state/attempt lookups and the progress snapshot group-by.
            models.Index(fields=["run_id", "kind", "version_item_id"], name="audit_event_run_kind_item_idx"),
        ]

    def __str__(self) -> str:
        return f"AuditEvent(run={self.run_id} {self.kind} vi={self.version_item_id})"


def trial_severities(result: dict) -> list:
    """Every judged trial in one stored result (one per repetition)."""
    reps = result.get("reps")
    if isinstance(reps, list):
        return [(r or {}).get("severity") for r in reps]
    return [result.get("severity")]


def trial_counts(status: str, result: dict | None) -> dict[str, int]:
    """Per-row trial tallies kept beside the (large) ``result`` JSON.

    Repetitions count as separate trials. ERROR severities and non-completed
    scenarios are excluded from ``n_judged`` and counted in ``n_errors``;
    "ungraded" trials (a judgment without a verdict) are counted apart.
    """
    out = {"n_pass": 0, "n_judged": 0, "n_errors": 0, "n_ungraded": 0}
    if status != "completed":
        out["n_errors"] = 1
        return out
    for sev in trial_severities(result or {}):
        if not sev or sev == "ERROR":
            out["n_errors"] += 1
        elif sev == "ungraded":
            out["n_ungraded"] += 1
        else:
            out["n_judged"] += 1
            if sev == "pass":
                out["n_pass"] += 1
    return out


def result_projection(result: dict | None) -> dict:
    """The few fields the run's Results list shows, lifted out of the big ``result``.

    Transcripts dominate a stored result; the list only needs verdicts and short
    summaries, so ``ScenarioResult.list_summary`` keeps just those.
    """
    r = result if isinstance(result, dict) else {}
    reps = r.get("reps")
    return {
        "severity": r.get("severity"),
        "aggregated_severity": r.get("aggregated_severity"),
        "summary": r.get("summary", ""),
        "agreement_rate": r.get("agreement_rate"),
        "severity_distribution": r.get("severity_distribution"),
        "has_reps": isinstance(reps, list),
        "reps": (
            [{"severity": x.get("severity"), "summary": x.get("summary", "")} for x in reps if isinstance(x, dict)]
            if isinstance(reps, list) else []
        ),
        "agentic_status": (r.get("agentic_evaluation") or {}).get("status", ""),
    }


class ScenarioResult(models.Model):
    """Idempotent final result for one scenario in one run."""

    run_id = models.PositiveBigIntegerField()
    version_item_id = models.CharField(max_length=64)
    status = models.CharField(max_length=32)
    attempts = models.PositiveIntegerField(default=1)
    # Full serialized SimpleAudit AuditResult (severity, conversation, tokens,
    # judgment, ...). Large raw transcripts may instead be offloaded to object
    # storage; this column holds the structured result record.
    result = models.JSONField(default=dict, blank=True)
    # Trial tallies derived from ``status`` + ``result`` (see ``trial_counts``).
    # They let run totals be a SUM over small columns instead of loading every
    # transcript; ``save()`` keeps them in step with the JSON.
    n_pass = models.PositiveIntegerField(default=0)
    n_judged = models.PositiveIntegerField(default=0)
    n_errors = models.PositiveIntegerField(default=0)
    n_ungraded = models.PositiveIntegerField(default=0)
    # Small projection of ``result`` for list pages (see ``result_projection``).
    list_summary = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    COUNT_FIELDS = ("n_pass", "n_judged", "n_errors", "n_ungraded", "list_summary")

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or {"status", "result"} & set(update_fields):
            for name, value in trial_counts(self.status, self.result).items():
                setattr(self, name, value)
            self.list_summary = result_projection(self.result)
            if update_fields is not None:
                kwargs["update_fields"] = [*update_fields, *self.COUNT_FIELDS]
        super().save(*args, **kwargs)

    class Meta:
        db_table = "core_scenario_result"
        constraints = [
            models.UniqueConstraint(
                fields=["run_id", "version_item_id"],
                name="unique_scenario_result_per_run_item",
            ),
        ]
        indexes = [
            models.Index(fields=["run_id", "status"], name="scenario_result_run_status_idx"),
        ]

    def __str__(self) -> str:
        return f"ScenarioResult(run={self.run_id} vi={self.version_item_id} {self.status})"


@retry_if_locked
def append_event(run_id: int | str, version_item_id: str, kind: str, payload: dict | None = None) -> int:
    """Append a durable event and return its id (usable as an SSE Last-Event-ID)."""
    event = AuditEvent.objects.create(
        run_id=int(run_id),
        version_item_id=version_item_id,
        kind=kind,
        payload=payload or {},
    )
    return event.id


@retry_if_locked
def append_events(run_id: int | str, version_item_id: str, items: list[tuple[str, dict | None]]) -> None:
    """Append several events in one INSERT, keeping their order (ids ascend)."""
    if not items:
        return
    AuditEvent.objects.bulk_create([
        AuditEvent(run_id=int(run_id), version_item_id=version_item_id, kind=kind, payload=payload or {})
        for kind, payload in items
    ])


# Per-turn progress events: by far the most numerous rows, and only useful
# while a run is live (the progress view and "Latest Activity"). State and
# terminal events are kept for good.
PRUNABLE_EVENT_KINDS = ("scenario_turn", "scenario_rep_started")


def prune_finished_run_events(older_than_days: int, batch_size: int = 5000) -> int:
    """Delete per-turn events of runs that finished more than ``older_than_days`` ago.

    Deletes in id batches so a large backlog never holds one long transaction.
    Returns the number of rows deleted.
    """
    from datetime import timedelta

    from django.utils import timezone

    from audits.models import AuditRun

    if older_than_days <= 0:
        return 0
    cutoff = timezone.now() - timedelta(days=older_than_days)
    finished = AuditRun.objects.filter(
        status__in=AuditRun.TERMINAL_STATUSES, finished_at__lt=cutoff
    ).values("id")
    qs = AuditEvent.objects.filter(run_id__in=finished, kind__in=PRUNABLE_EVENT_KINDS)
    deleted = 0
    while True:
        ids = list(qs.order_by().values_list("id", flat=True)[:batch_size])
        if not ids:
            return deleted
        deleted += AuditEvent.objects.filter(id__in=ids).delete()[0]


def _event_dict(e: AuditEvent) -> dict:
    return {
        "id": e.id,
        "run_id": e.run_id,
        "version_item_id": e.version_item_id,
        "kind": e.kind,
        "payload": e.payload,
        "created_at": e.created_at.isoformat(),
    }


def list_events(run_id: int | str, after_id: int = 0, limit: int | None = None) -> list[dict]:
    """Return durable events for a run with id > after_id, oldest first.

    Mirrors SSE replay semantics: a client reconnecting with a
    ``Last-Event-ID`` passes it as ``after_id`` to fetch only what it missed.
    ``limit`` caps the batch so a large backlog is streamed in chunks.
    """
    qs = AuditEvent.objects.filter(run_id=int(run_id), id__gt=after_id).order_by("id")
    if limit is not None:
        qs = qs[:limit]
    return [_event_dict(e) for e in qs]


# Per-scenario event kinds whose most recent occurrence fully determines where
# that scenario currently is (queued / in rep R at turn T / judging / done).
SCENARIO_STATE_KINDS = (
    "scenario_attempted",
    "scenario_rep_started",
    "scenario_turn",
    "scenario_completed",
    "scenario_failed",
    "scenario_skipped_existing",
)


def progress_snapshot(run_id: int | str) -> tuple[list[dict], int]:
    """Latest state event per scenario, plus the run's highest event id.

    Lets the live progress page start from current state and stream only newer
    events, instead of replaying the whole log (1000 scenarios x 20 reps is
    hundreds of thousands of events).
    """
    from django.db.models import Max

    run_id = int(run_id)
    latest_ids = (
        AuditEvent.objects.filter(run_id=run_id, kind__in=SCENARIO_STATE_KINDS)
        .values("version_item_id")
        .annotate(last_id=Max("id"))
        .values_list("last_id", flat=True)
    )
    ids = list(latest_ids)
    # A terminal run event that landed just before the page rendered must not
    # be skipped, or the page would wait forever for it.
    terminal_id = (
        AuditEvent.objects.filter(run_id=run_id, kind__in=("run_completed", "run_failed", "run_cancelled"))
        .aggregate(m=Max("id"))["m"]
    )
    if terminal_id:
        ids.append(terminal_id)
    # Stage too (the run row's status can lag behind the event log): one event
    # per distinct stage, since stages can be logged out of order; the client
    # only moves the stage forward, so it settles on the furthest one.
    ids += list(
        AuditEvent.objects.filter(run_id=run_id, kind="run_stage")
        .values("payload__stage")
        .annotate(last_id=Max("id"))
        .values_list("last_id", flat=True)
    )
    # The last few activity events seed "Latest Activity"; replayed in id
    # order, so each scenario's newest state still wins.
    ids += list(
        AuditEvent.objects.filter(
            run_id=run_id,
            kind__in=("scenario_turn", "scenario_rep_started", "scenario_completed", "scenario_failed"),
        )
        .order_by("-id")
        .values_list("id", flat=True)[:4]
    )
    events = [_event_dict(e) for e in AuditEvent.objects.filter(id__in=set(ids)).order_by("id")]
    last_id = AuditEvent.objects.filter(run_id=run_id).aggregate(m=Max("id"))["m"] or 0
    return events, last_id


@retry_if_locked
@transaction.atomic
def upsert_scenario_result(
    run_id: int | str,
    version_item_id: str,
    *,
    status: str,
    attempts: int,
    result: dict | None = None,
) -> None:
    """Idempotently write/update the final result for a scenario in a run.

    Uses ``ON CONFLICT DO UPDATE`` semantics via get_or_create + MAX(attempts) so
    re-execution never creates a duplicate row and never lowers the attempt count.
    When ``result`` is provided it replaces the stored structured result; when
    omitted the existing result (if any) is preserved.
    """
    from django.db import IntegrityError

    run_id = int(run_id)
    existing = ScenarioResult.objects.select_for_update().filter(run_id=run_id, version_item_id=version_item_id).first()
    if existing is None:
        try:
            # Savepoint: a concurrent writer may insert first (unique key).
            with transaction.atomic():
                ScenarioResult.objects.create(
                    run_id=run_id,
                    version_item_id=version_item_id,
                    status=status,
                    attempts=attempts,
                    result=result or {},
                )
            return
        except IntegrityError:
            existing = ScenarioResult.objects.select_for_update().get(run_id=run_id, version_item_id=version_item_id)
    existing.attempts = max(existing.attempts, attempts)
    if existing.status == "completed" and status != "completed":
        # A duplicate execution (retry, sweeper resubmission) that failed must
        # never replace a success another copy already stored.
        existing.save(update_fields=["attempts", "updated_at"])
        return
    existing.status = status
    if result is not None:
        existing.result = result
    existing.save(update_fields=["status", "attempts", "result", "updated_at"])


def count_results(run_id: int | str) -> int:
    return ScenarioResult.objects.filter(run_id=int(run_id)).count()


def get_result(run_id: int | str, version_item_id: str) -> dict | None:
    row = ScenarioResult.objects.filter(run_id=int(run_id), version_item_id=version_item_id).first()
    if row is None:
        return None
    return {
        "run_id": row.run_id,
        "version_item_id": row.version_item_id,
        "status": row.status,
        "attempts": row.attempts,
        "result": row.result,
    }
