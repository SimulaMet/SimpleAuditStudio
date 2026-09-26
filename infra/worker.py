"""Production SimpleAudit worker driven by Hatchet (external server mode).

This is the production counterpart of ``spike/worker.py``. The spike validated the
durable-job acceptance criteria against an embedded engine with a SQLite stand-in;
this module wires the same task shape to the real components:

- PostgreSQL domain tables are the source of truth (``AuditRun``,
  ``ScenarioSetVersionItem``). Progress is written as durable ``AuditEvent`` rows
  (see ``core.audit_events``), which is what SSE replays from.
- The web/API process never executes model calls; only this worker does.
- Cancellation is observed from a durable flag on the run, not process memory, so
  it survives web-process restarts.
- Final per-scenario results are written idempotently keyed on
  ``(run_id, version_item_id)`` so duplicate execution cannot create duplicates.
- The engine provenance guard fails the run on a SimpleAudit version/commit
  mismatch between the frozen run manifest and the worker's loaded engine.

Real Target -> Auditor -> Judge execution is wired through ``core.engine``: each
scenario builds a ``ModelAuditor`` from the frozen endpoint snapshots on the run
and calls ``run_scenario`` once. The engine is imported lazily, so if it is not
installed (e.g. in a web-only process or a test without the engine package)
the scenario fails with a recorded ``EngineError`` rather than crashing import.
The durable plumbing — retries, cancellation, idempotency, progress events,
version guard — is fully live and mirrors the validated spike.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from hatchet_sdk import ClientConfig, Context, Hatchet, Worker
from hatchet_sdk.config import ClientTLSConfig
from pydantic import BaseModel

from infra.simpleaudit_package import resolve_engine_provenance

logger = logging.getLogger(__name__)

# Engine provenance the worker "loaded", resolved from the installed SimpleAudit
# package metadata (version) and its PEP 610 direct_url commit (optional). This
# replaces the old SIMPLEAUDIT_VERSION / SIMPLEAUDIT_GIT_COMMIT env vars: the
# worker can no longer be told a provenance that differs from what it actually
# has installed, which is what makes the finalize guard meaningful.
_PROVENANCE = resolve_engine_provenance()
WORKER_SIMPLEAUDIT_VERSION = _PROVENANCE.version or ""
WORKER_GIT_COMMIT = _PROVENANCE.commit or ""


class ScenarioInput(BaseModel):
    run_id: str
    version_item_id: str
    attempt: int = 1


class FinalizeInput(BaseModel):
    run_id: str
    simpleaudit_version: str | None = None
    git_commit: str | None = None
    # Total number of scenarios pinned into this run. The finalize step uses it
    # to enforce the ordering guarantee: it must not mark the run completed until
    # every scenario has a durable result row.
    total_scenarios: int = 0


_CLIENT: Hatchet | None = None


def get_client() -> Hatchet:
    """Return the single shared Hatchet client for this process.

    In demo mode, returns the embedded Hatchet client (started by the CLI).
    Otherwise connects to the external Hatchet server.
    """
    global _CLIENT
    if _CLIENT is None:
        # Demo mode: reuse the embedded client started by the CLI entry point.
        from infra.minimal_config import get_embedded_client, is_minimal_config

        if is_minimal_config():
            embedded = get_embedded_client()
            if embedded is not None:
                _CLIENT = embedded
                return _CLIENT
            raise RuntimeError(
                "Demo mode active but embedded Hatchet client not started. "
                "Use 'simpleaudit-studio' CLI to launch the full stack."
            )

        # hatchet-sdk ClientConfig field names (verified against the installed
        # SDK): `server_url` is the HTTP API base, `host_port` is the gRPC
        # endpoint as "host:port", and `token` is the worker API token. Passing
        # any other key name is silently ignored by pydantic-settings, so these
        # must match exactly.
        #
        # TLS: the compose deployment runs a plaintext gRPC endpoint
        # (SERVER_GRPC_INSECURE=t), so the default strategy is "none" which makes
        # the SDK open an insecure channel. TLS/mTLS deployments set
        # HATCHET_TLS_STRATEGY accordingly (cert paths via HATCHET_CLIENT_TLS_*).
        tls_strategy = settings.HATCHET_TLS_STRATEGY or "none"
        config = ClientConfig(
            server_url=settings.HATCHET_SERVER_URL,
            host_port=settings.HATCHET_GRPC_URL,
            token=_resolve_hatchet_token(),
            tls_config=ClientTLSConfig(strategy=tls_strategy),
        )
        _CLIENT = Hatchet(config=config)
    return _CLIENT


def _resolve_hatchet_token() -> str | None:
    """Resolve the worker API token.

    Priority:
      1. ``HATCHET_API_KEY`` env var (explicit operator-provided token, e.g. for
         the auth-enabled hatchet-lite image).
      2. A shared token file (``HATCHET_TOKEN_FILE``), which the auth-disabled
         hatchet-lite-dev image writes to its config volume. Mounting that volume
         read-only into the worker makes a fresh ``docker compose up`` work
         turnkey without baking a per-instance JWT into the repo.
    """
    explicit = os.environ.get("HATCHET_API_KEY", "").strip()
    if explicit:
        return explicit
    token_file = os.environ.get("HATCHET_TOKEN_FILE", "").strip()
    if token_file and os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            logger.exception("failed to read HATCHET_TOKEN_FILE=%s", token_file)
    return None


def _is_cancelled(run_id: str) -> bool:
    """Cancellation is durable: read from the run row, never process memory."""
    from audits.models import AuditRun

    try:
        run = AuditRun.objects.select_related().get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        return False
    return run.status == AuditRun.Status.CANCELLED


def _scenario_execute_impl(workflow_input: ScenarioInput, ctx: Context) -> dict:
    """Execute one scenario for an audit run.

    Durable, idempotent, cancellation-aware. Real model execution is a
    placeholder until the SimpleAudit engine integration lands.
    """
    from audits.events import append_event, upsert_scenario_result
    from audits.models import AuditRun

    run_id = workflow_input.run_id
    version_item_id = workflow_input.version_item_id
    attempt = workflow_input.attempt

    # Graceful no-op if the run row was deleted (e.g. purged) while its
    # Hatchet tasks were still pending. Matches the pattern in
    # _run_finalize_impl and _is_cancelled.
    try:
        AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        logger.warning("Scenario task for missing run %s — skipping", run_id)
        return {"status": "missing"}

    # Optional fault injection for recovery tests: fail the first N EXECUTIONS.
    # Hatchet retries re-run the task with the SAME input (the `attempt` field
    # does NOT increment), so count executions via durable events, not `attempt`.
    fail_times = int(os.environ.get("AUDIT_FAIL_TIMES", "0"))
    if fail_times > 0:
        executions = sum(
            1
            for e in _iter_attempted_events(run_id, version_item_id)
        )
        if executions <= fail_times:
            raise RuntimeError(
                f"injected failure for {version_item_id} (execution {executions}/{fail_times})"
            )

    # Idempotency: if this scenario already has a successful durable result,
    # skip re-execution. Failed results are NOT skipped — they should be
    # retried on re-submission. This makes workflow re-submission safe (e.g.,
    # after a worker restart killed in-flight tasks) without wasting API calls.
    from audits.events import ScenarioResult as _SR
    _existing = _SR.objects.filter(
        run_id=int(run_id), version_item_id=str(version_item_id), status="completed"
    ).first()
    if _existing is not None:
        append_event(run_id, version_item_id, "scenario_skipped_existing", {})
        return {"status": "skipped_existing"}

    append_event(run_id, version_item_id, "scenario_attempted", {"attempt": attempt})
    append_event(run_id, "_run", "run_stage", {"stage": "target_execution"})

    if _is_cancelled(run_id):
        append_event(run_id, version_item_id, "scenario_skipped_cancelled", {})
        return {"status": "cancelled"}

    # --- Real Target -> Auditor -> Judge execution via the SimpleAudit engine --
    # All inputs come from the FROZEN AuditRun row (snapshots + pinned scenario
    # revision), never from live registry rows. Secrets are resolved from the
    # environment at execution time using each snapshot's secret_reference.
    run = AuditRun.objects.select_related("scenario_set_version").get(pk=int(run_id))
    item = run.scenario_set_version.items.get(pk=int(version_item_id))
    revision = item.revision

    # Stamp started_at on the first scenario that actually executes. A
    # conditional update keeps this idempotent and race-safe when several
    # scenario tasks start concurrently (only the first wins).
    if run.started_at is None:
        AuditRun.objects.filter(pk=run.pk, started_at__isnull=True).update(
            started_at=timezone.now()
        )

    from infra.engine import EngineError, run_scenario_repeated
    from infra.engine import run_scenario as engine_run_scenario

    gen_params = run.generation_parameters_snapshot or {}
    n_reps = int(gen_params.get("n_repetitions") or 1)
    max_turns = int(gen_params.get("max_turns") or 5)

    # Granular stage detail for the frontend: which phase of the scenario is
    # starting (target execution begins with the auditor generating a probe).
    append_event(
        run_id,
        "_run",
        "run_stage",
        {
            "stage": "target_execution",
            "detail": f"Turn 1/{max_turns} — Auditor generating probe",
        },
    )

    # --- Turn-level progress collection -------------------------------------
    # The engine invokes callbacks from inside asyncio.run(); Django ORM writes
    # (append_event) cannot happen there. So turn events are collected into a
    # list during async execution and flushed in sync context afterward.
    pending_events: list[tuple[str, dict]] = []
    # Mutable holder for the active rep index so _on_turn can stamp each turn
    # with the correct rep number.
    current_rep = [0]

    def _on_turn(turn_index: int, max_t: int, role: str) -> None:
        """Collect a per-turn progress event (flushed after asyncio.run())."""
        pending_events.append((
            "scenario_turn",
            {
                "turn": turn_index,
                "max_turns": max_t,
                "role": role,
                "rep": current_rep[0],
                "total_reps": n_reps,
            },
        ))

    def _on_rep_started(rep_idx: int) -> None:
        """Track the active rep so turn events carry the right rep number."""
        current_rep[0] = rep_idx + 1
        pending_events.append((
            "scenario_rep_started",
            {"rep": rep_idx + 1, "total_reps": n_reps},
        ))

    def _on_rep_done(rep_idx: int, rep_result: dict) -> None:
        """Emit a progress event after each repetition completes."""
        append_event(run_id, version_item_id, "scenario_rep_completed", {
            "rep": rep_idx + 1, "total": n_reps, "severity": rep_result.get("severity", ""),
        })

    def _flush_pending_events() -> None:
        """Flush collected turn/rep-started events (sync context only)."""
        for kind, payload in pending_events:
            append_event(run_id, version_item_id, kind, payload)
        pending_events.clear()

    try:
        if n_reps > 1:
            result_payload = run_scenario_repeated(
                name=item.scenario.key,
                description=revision.description,
                expected_behavior=revision.expected_behavior or None,
                test_prompt=revision.test_prompt or None,
                target=run.target_config_snapshot,
                auditor=run.auditor_config_snapshot,
                judge=run.judge_config_snapshot,
                generation=gen_params,
                n_repetitions=n_reps,
                on_rep_done=_on_rep_done,
                on_turn=_on_turn,
                on_rep_started=_on_rep_started,
            )
            # Use aggregated severity for the run-level counter
            severity = result_payload.get("aggregated_severity", "")
        else:
            result_payload = engine_run_scenario(
                name=item.scenario.key,
                description=revision.description,
                expected_behavior=revision.expected_behavior or None,
                test_prompt=revision.test_prompt or None,
                target=run.target_config_snapshot,
                auditor=run.auditor_config_snapshot,
                judge=run.judge_config_snapshot,
                generation=gen_params,
                on_turn=_on_turn,
            )
            severity = result_payload.get("severity", "")
        # Flush turn-level events collected during async execution. Safe here:
        # we're back in sync context after asyncio.run() returned.
        _flush_pending_events()
    except EngineError as exc:
        # A load/config failure is a hard error for this scenario: record it and
        # let Hatchet retry per policy. Do not swallow — the run must reflect it.
        append_event(run_id, version_item_id, "scenario_failed", {"error": str(exc)})
        with transaction.atomic():
            pre_existing = _result_row_exists(run_id, version_item_id)
            upsert_scenario_result(run_id, version_item_id, status="failed", attempts=attempt, result={"error": str(exc)})
            _bump_run_counters(run_id, version_item_id, succeeded=False, pre_existing=pre_existing)
        raise

    severity = result_payload.get("severity", "")
    failed = severity.upper() == "ERROR"
    with transaction.atomic():
        pre_existing = _result_row_exists(run_id, version_item_id)
        upsert_scenario_result(
            run_id, version_item_id, status="failed" if failed else "completed", attempts=attempt, result=result_payload
        )
        _bump_run_counters(run_id, version_item_id, succeeded=not failed, pre_existing=pre_existing)

    append_event(
        run_id,
        version_item_id,
        "scenario_completed" if not failed else "scenario_failed",
        {"attempt": attempt, "severity": severity},
    )
    return {"status": "failed" if failed else "completed", "attempt": attempt, "severity": severity}


def _iter_attempted_events(run_id: str, version_item_id: str):
    from audits.events import list_events

    return [e for e in list_events(run_id) if e["version_item_id"] == version_item_id and e["kind"] == "scenario_attempted"]


def _result_row_exists(run_id: str, version_item_id: str) -> bool:
    """Whether a durable ScenarioResult row already exists for this item.

    Must be called BEFORE upserting the result so the counter bump can tell a
    first execution (bump) apart from a retry (adjust only).
    """
    from audits.events import ScenarioResult

    return ScenarioResult.objects.filter(
        run_id=int(run_id), version_item_id=version_item_id
    ).exists()


def _bump_run_counters(
    run_id: str, version_item_id: str, *, succeeded: bool, pre_existing: bool
) -> None:
    """Idempotently bump run counters for a given version item.

    ``pre_existing`` must be captured by the caller BEFORE upserting the
    ScenarioResult row: it tells us whether a durable result already existed
    before this execution. First execution (no prior row) increments the
    counters; a retry (prior row present) only adjusts pass/fail if the
    outcome changed, never double-counting.
    """
    from audits.models import AuditRun

    try:
        run = AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        return

    if pre_existing:
        # Re-execution (retry): the counter was already bumped on the first
        # execution. Adjust pass/fail only if the outcome changed. The stored
        # status is still the PREVIOUS attempt's (the upsert runs after this
        # check in the caller), so read it directly.
        from audits.events import ScenarioResult

        existing = ScenarioResult.objects.filter(
            run_id=int(run_id), version_item_id=version_item_id
        ).first()
        was_success = existing is not None and existing.status == "completed"
        if was_success != succeeded:
            if succeeded:
                run.failed_scenarios = max(0, run.failed_scenarios - (1 if not was_success else 0))
                run.successful_scenarios += 1
            else:
                run.successful_scenarios = max(0, run.successful_scenarios - (1 if was_success else 0))
                run.failed_scenarios += 1
            run.save(update_fields=["completed_scenarios", "successful_scenarios", "failed_scenarios"])
        return

    run.completed_scenarios += 1
    if succeeded:
        run.successful_scenarios += 1
    else:
        run.failed_scenarios += 1
    run.save(update_fields=["completed_scenarios", "successful_scenarios", "failed_scenarios"])


def _run_finalize_impl(workflow_input: FinalizeInput, ctx: Context) -> dict:
    """Finalize an audit run once every scenario has a durable result.

    Enforces two invariants before writing the terminal state:
      1. Engine provenance guard (frozen version/commit must match the worker).
      2. Ordering guarantee: the run is only marked completed when the number of
         durable ``ScenarioResult`` rows equals the pinned scenario count. Because
         finalize is triggered as an independent task (not a dependent workflow
         step), it can be scheduled before slow scenarios finish; in that case we
         raise so Hatchet retries it later rather than recording a false
         ``completed`` with missing results.
    """
    from audits.events import append_event
    from audits.models import AuditRun

    run_id = workflow_input.run_id
    expected_version = workflow_input.simpleaudit_version
    expected_commit = workflow_input.git_commit

    # Version is authoritative provenance: a mismatch means the worker's engine
    # differs from what the run was frozen against, so the run must fail.
    if expected_version and expected_version != WORKER_SIMPLEAUDIT_VERSION:
        append_event(run_id, "_run", "run_failed", {"code": "SIMPLEAUDIT_VERSION_MISMATCH"})
        _mark_run_failed(run_id, "SIMPLEAUDIT_VERSION_MISMATCH")
        raise RuntimeError("SIMPLEAUDIT_VERSION_MISMATCH")
    # Commit is optional provenance (absent for registry installs). It is only
    # enforced when BOTH the frozen manifest and the loaded engine carry a
    # commit; otherwise there is nothing comparable and we do not fail the run.
    if expected_commit and WORKER_GIT_COMMIT and expected_commit != WORKER_GIT_COMMIT:
        append_event(run_id, "_run", "run_failed", {"code": "SIMPLEAUDIT_VERSION_MISMATCH"})
        _mark_run_failed(run_id, "SIMPLEAUDIT_VERSION_MISMATCH")
        raise RuntimeError("SIMPLEAUDIT_VERSION_MISMATCH")

    if _is_cancelled(run_id):
        append_event(run_id, "_run", "run_cancelled", {})
        return {"status": "cancelled"}

    # Idempotency: if the run already reached a terminal state, do nothing.
    try:
        current = AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        return {"status": "missing"}
    if current.status in (AuditRun.Status.COMPLETED, AuditRun.Status.FAILED, AuditRun.Status.CANCELLED):
        return {"status": current.status.value if hasattr(current.status, "value") else str(current.status)}

    # Ordering guarantee: wait until every pinned scenario has a result row.
    # Poll briefly before raising so Hatchet's retry backoff isn't the only
    # mechanism covering tail latency of slow scenarios.
    import time as _time

    done = _count_results(run_id)
    total = workflow_input.total_scenarios or current.total_scenarios or 0
    if total and done < total:
        deadline = _time.monotonic() + 30.0
        while done < total and _time.monotonic() < deadline:
            _time.sleep(2.0)
            done = _count_results(run_id)
        if done < total:
            append_event(run_id, "_run", "finalize_waiting", {"done": done, "total": total})
            raise RuntimeError(f"finalize premature: {done}/{total} scenarios have results")

    append_event(run_id, "_run", "run_stage", {"stage": "aggregation"})
    append_event(run_id, "_run", "run_completed", {"scenarios": done})
    _mark_run_completed(run_id)
    return {"status": "completed", "scenarios": done}


def _count_results(run_id: str) -> int:
    from audits.events import count_results

    return count_results(run_id)


def _mark_run_failed(run_id: str, code: str) -> None:
    from audits.models import AuditRun

    try:
        run = AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        return
    run.status = AuditRun.Status.FAILED
    run.error_code = code
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "error_code", "finished_at"])


def _mark_run_completed(run_id: str) -> None:
    from audits.models import AuditRun

    try:
        run = AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        return
    run.status = AuditRun.Status.COMPLETED
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at"])


def submit_run_workflow(run_id: str, version_item_ids: list[str], *, simpleaudit_version: str | None, git_commit: str | None):
    """Enqueue the per-run audit tasks (one scenario task each + a finalize task).

    Uses the two standalone tasks registered on the worker (``audit.scenario_execute``
    and ``audit.run_finalize``). Standalone tasks are the reliable primitive here:
    the worker subscribes to a fixed set of action names at startup, so dynamically
    built per-run workflow steps (which would need their own queue subscriptions)
    are never dispatched.

    Ordering guarantee: the finalize task is enqueued alongside the scenario tasks
    and may be scheduled before slow scenarios finish. That is safe because
    ``_run_finalize_impl`` refuses to mark the run completed until every pinned
    scenario has a durable result row, retrying via Hatchet until they do. A run
    therefore can never reach ``completed`` with missing results.

    Returns the finalize task run reference, or raises if the client is unavailable.
    """
    client = get_client()
    scenario_task = client.task(
        name="audit.scenario_execute",
        input_validator=ScenarioInput,
        retries=2,
        backoff_factor=2.0,
        execution_timeout="300s",
    )(_scenario_execute_impl)
    finalize_task = client.task(
        name="audit.run_finalize",
        input_validator=FinalizeInput,
        execution_timeout="60s",
        retries=10,
        backoff_factor=2.0,
    )(_run_finalize_impl)

    for vid in version_item_ids:
        scenario_task.run(
            input=ScenarioInput(run_id=str(run_id), version_item_id=str(vid), attempt=1),
            wait_for_result=False,
        )
    return finalize_task.run(
        input=FinalizeInput(
            run_id=str(run_id),
            simpleaudit_version=simpleaudit_version,
            git_commit=git_commit,
            total_scenarios=len(version_item_ids),
        ),
        wait_for_result=False,
    )


def build_worker() -> Worker:
    """Build the Hatchet worker bound to the configured pool label.

    Registers the two standalone task handlers (``audit.scenario_execute`` and
    ``audit.run_finalize``). These are the only actions the worker subscribes to,
    and they are exactly what ``submit_run_workflow`` enqueues for each audit run
    (one scenario task per pinned scenario plus a finalize task). Keeping the
    action set fixed at startup is what makes dispatch reliable — see the note in
    ``submit_run_workflow`` about why dynamic per-run workflows are not used.
    """
    client = get_client()
    scenario_task = client.task(
        name="audit.scenario_execute",
        input_validator=ScenarioInput,
        retries=2,
        backoff_factor=2.0,
        execution_timeout="300s",
    )(_scenario_execute_impl)
    # Finalize retries because it may be scheduled before slow scenarios finish;
    # _run_finalize_impl raises until every pinned scenario has a durable result.
    # With backoff_factor=2.0 and the default base delay, 10 retries span several
    # minutes, comfortably covering typical scenario completion times.
    finalize_task = client.task(
        name="audit.run_finalize",
        input_validator=FinalizeInput,
        execution_timeout="60s",
        retries=10,
        backoff_factor=2.0,
    )(_run_finalize_impl)
    return Worker(
        name=f"simpleaudit-audit-worker-{settings.WORKER_POOL}",
        config=client.config,
        slot_config={"default": 4},
        labels={"pool": settings.WORKER_POOL},
        workflows=[scenario_task, finalize_task],
    )


def _recover_stuck_runs() -> None:
    """Re-submit audit runs that were left in-flight when the worker died.

    Called once at worker startup, before the task loop begins. Finds runs in a
    non-terminal state (queued through report_generation) whose scenario tasks
    are no longer active in Hatchet (because the previous worker process was
    killed) and re-enqueues them. This is safe because scenario tasks are
    idempotent: already-completed scenarios skip instantly via the durable
    result check.

    Only recovers runs that have been stuck for more than a short grace period
    to avoid racing with a concurrent healthy worker.
    """
    from django.utils import timezone as dj_timezone

    from audits.models import AuditRun
    from scenarios.models import ScenarioSetVersionItem

    grace = dj_timezone.now() - __import__("datetime").timedelta(seconds=30)
    stuck = AuditRun.objects.filter(
        status__in=[
            AuditRun.Status.QUEUED,
            AuditRun.Status.PREPARING,
            AuditRun.Status.TARGET_EXECUTION,
            AuditRun.Status.AUDITING,
            AuditRun.Status.JUDGING,
            AuditRun.Status.AGGREGATION,
            AuditRun.Status.REPORT_GENERATION,
        ],
        updated_at__lt=grace,
        archived=False,
    )

    recovered = 0
    for run in stuck:
        try:
            items = list(
                ScenarioSetVersionItem.objects.filter(version=run.scenario_set_version)
            )
            vids = [str(it.pk) for it in items]
            if not vids:
                continue
            submit_run_workflow(
                str(run.pk),
                vids,
                simpleaudit_version=run.simpleaudit_version,
                git_commit=run.git_commit,
            )
            recovered += 1
            logger.info("Crash recovery: re-submitted run %s (%d scenarios)", run.pk, len(vids))
        except Exception as exc:  # noqa: BLE001 - crash recovery must not fail the whole sweep
            logger.warning("Crash recovery: failed to re-submit run %s: %s", run.pk, exc)

    if recovered:
        logger.info("Crash recovery: re-submitted %d stuck run(s)", recovered)


def start_worker(max_startup_retries: int = 30, startup_retry_delay: float = 2.0) -> None:
    """Blocking entrypoint used by ``manage.py run_worker``.

    Retries client construction for a bounded period so the worker tolerates the
    Hatchet server (or its auth-disabled token file) coming up slightly after this
    process starts, instead of crash-looping on the very first attempt. Once the
    client builds successfully, crash-recovery re-submits any runs left in-flight
    by a previous worker death, then ``worker.start()`` blocks for the process
    lifetime.
    """
    import time

    last_error: Exception | None = None
    for attempt in range(1, max_startup_retries + 1):
        try:
            worker = build_worker()
            break
        except Exception as exc:  # noqa: BLE001 - any startup failure is retryable
            last_error = exc
            logger.warning(
                "Worker startup attempt %d/%d failed: %s: %s",
                attempt, max_startup_retries, type(exc).__name__, exc,
            )
            if attempt < max_startup_retries:
                time.sleep(startup_retry_delay)
    else:
        raise RuntimeError(
            f"Worker could not connect to Hatchet after {max_startup_retries} attempts"
        ) from last_error

    # Re-submit runs orphaned by a previous worker crash/restart.
    try:
        _recover_stuck_runs()
    except Exception as exc:  # noqa: BLE001 - crash recovery must not block worker startup
        logger.warning("Crash recovery skipped: %s", exc)

    print(f"Starting SimpleAudit worker (pool={settings.WORKER_POOL})...", flush=True)
    worker.start()
