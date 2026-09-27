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

    run_id = models.PositiveBigIntegerField(db_index=True)
    version_item_id = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_audit_event"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["run_id", "id"], name="audit_event_run_id_idx"),
        ]

    def __str__(self) -> str:
        return f"AuditEvent(run={self.run_id} {self.kind} vi={self.version_item_id})"


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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_scenario_result"
        constraints = [
            models.UniqueConstraint(
                fields=["run_id", "version_item_id"],
                name="unique_scenario_result_per_run_item",
            ),
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
