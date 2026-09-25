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


def append_event(run_id: int | str, version_item_id: str, kind: str, payload: dict | None = None) -> int:
    """Append a durable event and return its id (usable as an SSE Last-Event-ID)."""
    event = AuditEvent.objects.create(
        run_id=int(run_id),
        version_item_id=version_item_id,
        kind=kind,
        payload=payload or {},
    )
    return event.id


def list_events(run_id: int | str, after_id: int = 0) -> list[dict]:
    """Return durable events for a run with id > after_id, oldest first.

    Mirrors SSE replay semantics: a client reconnecting with a
    ``Last-Event-ID`` passes it as ``after_id`` to fetch only what it missed.
    """
    qs = AuditEvent.objects.filter(run_id=int(run_id), id__gt=after_id).order_by("id")
    return [
        {
            "id": e.id,
            "run_id": e.run_id,
            "version_item_id": e.version_item_id,
            "kind": e.kind,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in qs
    ]


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
    run_id = int(run_id)
    existing = ScenarioResult.objects.filter(run_id=run_id, version_item_id=version_item_id).first()
    if existing is None:
        ScenarioResult.objects.create(
            run_id=run_id,
            version_item_id=version_item_id,
            status=status,
            attempts=attempts,
            result=result or {},
        )
        return
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
