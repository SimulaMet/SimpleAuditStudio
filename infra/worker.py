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
import threading as _threading
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from hatchet_sdk import ClientConfig, Context, Hatchet, Worker
from hatchet_sdk.config import ClientTLSConfig
from pydantic import BaseModel

from infra.db import retry_if_locked, with_fresh_connection
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


# Scenario execution budget. Hatchet retries a failing task SCENARIO_RETRIES
# times; attempts are counted durably (scenario_attempted events), so the budget
# also holds across resubmissions (crash recovery, resume). A failure on the
# last attempt is final; earlier failures are provisional and retried.
SCENARIO_RETRIES = 2
MAX_SCENARIO_ATTEMPTS = SCENARIO_RETRIES + 1

# A run with no progress events for this long is resumed: its missing
# scenarios are resubmitted. Resubmission is idempotent (see the per-scenario
# concurrency key on the task and the durable result checks), so resuming a
# run that is merely slow or queued behind other runs is harmless.
RESUME_STALE_MINUTES = 10
# A run that never reached Hatchet (e.g. the launching process died mid-submit)
# is submitted by the sweeper after this grace period instead of waiting
# RESUME_STALE_MINUTES like a merely slow run.
NEVER_SUBMITTED_GRACE_SECONDS = 60

# Errors a retry cannot fix (wrong URL, model id or key): the first failure is final.
_PERMANENT_ERROR_MARKERS = (
    "NotFoundError", "AuthenticationError", "PermissionDeniedError", "BadRequestError",
    "Error code: 400", "Error code: 401", "Error code: 403", "Error code: 404",
    "invalid_api_key", "model_not_found",
)


def _is_permanent_error(text: str) -> bool:
    return any(marker in (text or "") for marker in _PERMANENT_ERROR_MARKERS)


class ScenarioAttemptFailed(RuntimeError):
    """Raised after a provisional failure is recorded, so Hatchet retries the task."""

_ACTIVE_STATUSES = (
    "queued", "preparing", "target_execution", "auditing", "judging",
    "aggregation", "report_generation",
)

_CLIENT: Hatchet | None = None


# Set when the shared client was built with a placeholder token because no real
# token was resolvable at import time. Cleared once a real token is resolved and
# the client rebuilt (see get_client).
_CLIENT_IS_PLACEHOLDER = False


def get_client() -> Hatchet:
    """Return the single shared Hatchet client for this process.

    In demo mode, returns the embedded Hatchet client (started by the CLI).
    In zero-Docker dev_server mode (--embedded), connects to the embedded
    engine via the HATCHET_EMBEDDED_HANDSHAKE env var set by the parent
    process — no sidecar restart needed. Otherwise connects to the external
    Hatchet server. If the client was originally built with a placeholder
    token (no real token available at import time) and a real token has since
    become available, rebuilds the client with it so task submission works
    without a process restart.
    """
    global _CLIENT, _CLIENT_IS_PLACEHOLDER
    if _CLIENT is None or (_CLIENT_IS_PLACEHOLDER and _resolve_hatchet_token()):
        # Demo mode: reuse the embedded client started by the CLI entry point.
        from infra.minimal_config import get_embedded_client, is_minimal_config

        if is_minimal_config():
            embedded = get_embedded_client()
            if embedded is not None:
                _CLIENT = embedded
                _CLIENT_IS_PLACEHOLDER = False
                return _CLIENT
            raise RuntimeError(
                "Demo mode active but embedded Hatchet client not started. "
                "Use 'simpleaudit-studio' CLI to launch the full stack."
            )

        # Zero-Docker dev_server mode: the parent process started the embedded
        # Hatchet engine and exported its handshake (token, gRPC address, API
        # URL) via HATCHET_EMBEDDED_HANDSHAKE. Build a lightweight client that
        # connects to the already-running engine — no sidecar restart.
        handshake_raw = os.environ.get("HATCHET_EMBEDDED_HANDSHAKE")
        if handshake_raw:
            from hatchet_sdk.embedded import Handshake as _Handshake

            hs = _Handshake.model_validate_json(handshake_raw)
            config = ClientConfig(
                token=hs.token,
                tenant_id=hs.tenant_id,
                host_port=hs.grpc_address,
                server_url=hs.api_url or None,
                tls_config=ClientTLSConfig(strategy="none"),
            )
            _CLIENT = Hatchet(config=config)
            _CLIENT_IS_PLACEHOLDER = False
            logger.info(
                "Connected to embedded Hatchet via handshake (grpc=%s)",
                hs.grpc_address,
            )
            return _CLIENT

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
        token = _resolve_hatchet_token()
        if not token:
            # No live token (e.g. the auth-disabled dev image hasn't written its
            # token file yet, or no HATCHET_API_KEY is set). Build a client with
            # a structurally-valid placeholder JWT so module-level task
            # registration can proceed; the gRPC channels are lazy and only fail
            # when actually used. get_client() rebuilds the client with a real
            # token once one becomes available.
            logger.warning(
                "No Hatchet token resolved (HATCHET_API_KEY / HATCHET_TOKEN_FILE); "
                "building client with placeholder token. Task submission will fail "
                "until a real token is available."
            )
            token = _placeholder_jwt(settings.HATCHET_SERVER_URL, settings.HATCHET_GRPC_URL)
            _CLIENT_IS_PLACEHOLDER = True
        else:
            _CLIENT_IS_PLACEHOLDER = False
        config = ClientConfig(
            server_url=settings.HATCHET_SERVER_URL,
            host_port=settings.HATCHET_GRPC_URL,
            token=token,
            tls_config=ClientTLSConfig(strategy=tls_strategy),
        )
        _CLIENT = Hatchet(config=config)
    return _CLIENT


def _placeholder_jwt(server_url: str, grpc_url: str) -> str:
    """Build a structurally-valid unsigned JWT for placeholder use.

    The SDK validates that the token starts with ``ey`` and parses its claims
    (``sub``, ``server_url``, ``grpc_broadcast_address``) at construction time.
    This placeholder satisfies that validation without any real credentials;
    actual gRPC calls with it will be rejected by the server.
    """
    import base64
    import json

    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "placeholder-tenant",
        "server_url": server_url,
        "grpc_broadcast_address": grpc_url,
    }).encode())
    signature = _b64(b"placeholder")
    return f"{header}.{payload}.{signature}"


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


# --- Task implementations ---------------------------------------------
# Defined before the module-level registration below so the registered tasks
# can reference these callables directly.

def _scenario_execute_impl(workflow_input: ScenarioInput, ctx: Context) -> dict:
    """Execute one scenario for an audit run.

    Durable, idempotent, cancellation-aware. Real model execution is a
    placeholder until the SimpleAudit engine integration lands.
    """
    from audits.events import append_event, upsert_scenario_result
    from audits.models import AuditRun

    run_id = workflow_input.run_id
    version_item_id = workflow_input.version_item_id

    # Graceful no-op if the run row was deleted (e.g. purged) while its
    # Hatchet tasks were still pending. Matches the pattern in
    # _run_finalize_impl and _is_cancelled.
    try:
        _run_row = AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        logger.warning("Scenario task for missing run %s — skipping", run_id)
        return {"status": "missing"}
    # A finished or cancelled run takes no more work (late duplicates, retries
    # queued before a cancel, resubmissions racing completion).
    if _run_row.status not in _ACTIVE_STATUSES:
        return {"status": "run_" + str(_run_row.status)}

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
    if _has_terminal_result(run_id, version_item_id):
        append_event(run_id, version_item_id, "scenario_skipped_existing", {})
        _maybe_finalize(run_id)
        return {"status": "skipped_existing"}

    # Durable attempt budget: counts every execution of this scenario, across
    # Hatchet retries AND resubmissions. Past the budget, give up for good
    # instead of burning model calls on a scenario that keeps crashing.
    attempt = _attempt_count(run_id, version_item_id) + 1
    if attempt > MAX_SCENARIO_ATTEMPTS:
        upsert_scenario_result(
            run_id, version_item_id, status="failed", attempts=attempt - 1,
            result={"error": f"Gave up after {attempt - 1} attempts"},
        )
        _sync_run_counters(run_id)
        append_event(run_id, version_item_id, "scenario_failed", {"error": f"Gave up after {attempt - 1} attempts"})
        _maybe_finalize(run_id)
        return {"status": "failed", "reason": "attempts_exhausted"}

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
    # Same pattern for status: leave queued/preparing once work actually runs,
    # so the Status card and run lists stop showing "Queued" mid-run.
    AuditRun.objects.filter(
        pk=run.pk, status__in=[AuditRun.Status.QUEUED, AuditRun.Status.PREPARING]
    ).update(status=AuditRun.Status.TARGET_EXECUTION)

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
    # (append_event) cannot happen there — Django raises SynchronousOnlyOperation
    # when a running event loop is present in the thread. So turn events are
    # pushed onto a thread-safe queue and a dedicated flusher thread appends
    # them to the durable event log IN REAL TIME. This is what lets the live
    # progress UI show scenarios mid-turn (turn/role occupancy) instead of only
    # after the whole scenario has finished.
    import queue as _queue
    import threading as _threading

    _event_queue: _queue.Queue = _queue.Queue()
    _FLUSH_SENTINEL = object()
    # Durable cancel flag, polled by the flusher (a sync thread that may use the
    # ORM) and read by the engine callbacks (which may not). A user cancel then
    # stops the engine at the next rep instead of running every remaining rep.
    _cancel_requested = _threading.Event()
    _CANCEL_POLL_S = 5.0

    def _flush_loop() -> None:
        from django.db import connections
        from django.db.utils import InterfaceError, OperationalError

        consecutive_errors = 0
        max_retries = 5
        try:
            while True:
                try:
                    item = _event_queue.get(timeout=_CANCEL_POLL_S)
                except _queue.Empty:
                    item = None
                if item is _FLUSH_SENTINEL:
                    break
                if not _cancel_requested.is_set():
                    try:
                        if _is_cancelled(run_id):
                            _cancel_requested.set()
                    except Exception:  # noqa: BLE001, S110 - polling is best effort
                        pass
                if item is None:
                    continue
                kind, payload = item
                for attempt in range(max_retries):
                    try:
                        append_event(run_id, version_item_id, kind, payload)
                        consecutive_errors = 0
                        break
                    except (OperationalError, InterfaceError) as exc:
                        consecutive_errors += 1
                        wait = min(2 ** attempt, 16)
                        logger.warning(
                            "Event flush failed (run=%s vi=%s kind=%s attempt %d/%d): %s — retrying in %ds",
                            run_id, version_item_id, kind, attempt + 1, max_retries, exc, wait,
                        )
                        if attempt < max_retries - 1:
                            _threading.Event().wait(wait)
                        else:
                            logger.error(
                                "Event flush permanently failed after %d attempts (run=%s vi=%s kind=%s): %s",
                                max_retries, run_id, version_item_id, kind, exc,
                            )
                            # Reset the broken connection so next event can reconnect.
                            try:
                                connections.close_all()
                            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                                pass
        finally:
            # This thread got its own thread-local DB connection; release it.
            connections.close_all()

    _flusher = _threading.Thread(target=_flush_loop, daemon=True)
    _flusher.start()
    # Mutable holder for the active rep index (0-based) so _on_turn can stamp
    # each turn with the correct rep number. Set by _on_rep_started, which the
    # engine wrapper fires right before each rep begins (see
    # run_scenario_repeated). Engine-internal retries within a rep re-emit
    # auditor turns but stay in the same rep.
    current_rep = [0]
    # Set when this task is cancelled (Hatchet cancel or the run's durable
    # cancel flag) so the engine stops at the next rep instead of running on
    # as an orphaned thread. Only touched from the engine's event loop.
    import asyncio as _asyncio
    _cancel_event = _asyncio.Event()

    def _check_cancel() -> None:
        if _cancel_event.is_set():
            return
        if getattr(ctx, "is_cancelled", False) or _cancel_requested.is_set():
            _cancel_event.set()

    def _on_turn(turn_index: int, max_t: int, role: str) -> None:
        """Queue a per-turn progress event (flushed live by the worker thread)."""
        _check_cancel()
        _event_queue.put((
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
        """Track the active rep; called from the engine's event loop (queue only)."""
        _check_cancel()
        current_rep[0] = rep_idx
        _event_queue.put((
            "scenario_rep_started",
            {"rep": rep_idx, "total_reps": n_reps},
        ))

    def _on_rep_done(rep_idx: int, rep_result: dict) -> None:
        """Emit a progress event after each repetition completes."""
        append_event(run_id, version_item_id, "scenario_rep_completed", {
            "rep": rep_idx + 1, "total": n_reps, "severity": rep_result.get("severity", ""),
        })

    _flusher_stopped = [False]

    def _stop_event_flusher() -> None:
        """Drain the queue and stop the flusher thread (sync context only).

        Joining here guarantees every turn event is durably written BEFORE the
        caller appends scenario_completed/failed, so SSE replay order is
        turn events first, terminal event last. Idempotent.
        """
        if _flusher_stopped[0]:
            return
        _flusher_stopped[0] = True
        _event_queue.put(_FLUSH_SENTINEL)
        _flusher.join(timeout=30)

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
                cancel_event=_cancel_event,
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
        # Stop the live flusher: drains any remaining turn events and joins the
        # thread so all turn events are durable before the terminal event below.
        _stop_event_flusher()
        if _cancel_event.is_set():
            # Stopped early: the reps are partial, so record nothing and let
            # Hatchet's retry (or the user's cancel) decide what happens next.
            append_event(run_id, version_item_id, "scenario_skipped_cancelled", {})
            return {"status": "cancelled"}
    except EngineError as exc:
        _stop_event_flusher()
        # A load/config/crash failure: record it durably. Before the last
        # attempt it is provisional and re-raised so Hatchet retries; on the
        # last attempt it is final and counts toward run completion.
        if _has_terminal_result(run_id, version_item_id, completed_only=True):
            # A concurrent duplicate already succeeded; its result stands.
            return {"status": "skipped_existing"}
        final = attempt >= MAX_SCENARIO_ATTEMPTS or _is_permanent_error(str(exc))
        append_event(run_id, version_item_id, "scenario_failed", {"error": str(exc), "final": final})
        upsert_scenario_result(
            run_id, version_item_id, status="failed",
            attempts=MAX_SCENARIO_ATTEMPTS if final else attempt, result={"error": str(exc)},
        )
        _sync_run_counters(run_id)
        if not final:
            raise
        _maybe_finalize(run_id)
        return {"status": "failed", "attempt": attempt}

    # A concurrent duplicate may have finished first; never overwrite it.
    if _has_terminal_result(run_id, version_item_id, completed_only=True):
        append_event(run_id, version_item_id, "scenario_skipped_existing", {})
        return {"status": "skipped_existing"}

    failed = severity.upper() == "ERROR"
    error = str((result_payload.get("judgment") or {}).get("error") or result_payload.get("summary") or "") if failed else ""
    # A failed attempt is final on the last attempt or for errors a retry can't fix;
    # otherwise it is provisional, stored as failed with attempts < max (so not terminal).
    final = not failed or attempt >= MAX_SCENARIO_ATTEMPTS or _is_permanent_error(error)
    upsert_scenario_result(
        run_id, version_item_id, status="failed" if failed else "completed",
        attempts=MAX_SCENARIO_ATTEMPTS if failed and final else attempt, result=result_payload,
    )
    _sync_run_counters(run_id)

    payload = {"attempt": attempt, "severity": severity}
    if failed:
        payload.update(error=error, final=final)
    append_event(run_id, version_item_id, "scenario_failed" if failed else "scenario_completed", payload)
    if not final:
        # Without this the task "succeeds", Hatchet never retries, and the run
        # waits for the idle sweeper (RESUME_STALE_MINUTES) to resubmit it.
        raise ScenarioAttemptFailed(f"attempt {attempt} failed: {error}")
    _maybe_finalize(run_id)
    return {"status": "failed" if failed else "completed", "attempt": attempt, "severity": severity}


def _iter_attempted_events(run_id: str, version_item_id: str):
    from audits.events import list_events

    return [e for e in list_events(run_id) if e["version_item_id"] == version_item_id and e["kind"] == "scenario_attempted"]


def _attempt_count(run_id: str, version_item_id: str) -> int:
    """Executions of this scenario so far (durable, survives resubmission)."""
    from audits.events import AuditEvent

    return AuditEvent.objects.filter(
        run_id=int(run_id), version_item_id=str(version_item_id), kind="scenario_attempted"
    ).count()


def _terminal_results(run_id: str):
    """Result rows that are final: completed, or failed with no attempts left."""
    from django.db.models import Q

    from audits.events import ScenarioResult

    return ScenarioResult.objects.filter(run_id=int(run_id)).filter(
        Q(status="completed") | Q(status="failed", attempts__gte=MAX_SCENARIO_ATTEMPTS)
    )


def _has_terminal_result(run_id: str, version_item_id: str, completed_only: bool = False) -> bool:
    from audits.events import ScenarioResult

    if completed_only:
        qs = ScenarioResult.objects.filter(run_id=int(run_id), status="completed")
    else:
        qs = _terminal_results(run_id)
    return qs.filter(version_item_id=str(version_item_id)).exists()


@retry_if_locked
def _sync_run_counters(run_id: str) -> None:
    """Recompute the run's counters from the durable result rows.

    Derived, not incremented: concurrent scenario completions, retries and
    duplicate executions can never double-count or lose an update.
    completed = final results; successful/failed = current row outcomes.
    """
    from audits.events import ScenarioResult
    from audits.models import AuditRun

    rows = ScenarioResult.objects.filter(run_id=int(run_id))
    AuditRun.objects.filter(pk=int(run_id)).update(
        completed_scenarios=_terminal_results(run_id).count(),
        successful_scenarios=rows.filter(status="completed").count(),
        failed_scenarios=rows.filter(status="failed").count(),
        updated_at=timezone.now(),
    )


def _provenance_mismatch(run) -> bool:
    """Frozen engine version/commit differs from the one this worker loaded."""
    if run.simpleaudit_version and run.simpleaudit_version != WORKER_SIMPLEAUDIT_VERSION:
        return True
    return bool(run.git_commit and WORKER_GIT_COMMIT and run.git_commit != WORKER_GIT_COMMIT)


@retry_if_locked
def _maybe_finalize(run_id: str) -> str | None:
    """Complete the run once every pinned scenario has a final result.

    Called after each scenario result (so the last scenario finalizes the run
    inline), by the sweeper, and by the legacy finalize task. The status flip is
    a conditional UPDATE, so exactly one caller wins and emits the terminal
    events. Returns the run's terminal status, or None if work remains.
    """
    from audits.events import append_event
    from audits.models import AuditRun

    try:
        run = AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        return None
    if run.status not in _ACTIVE_STATUSES:
        return str(run.status)
    done = _terminal_results(run_id).count()
    if not run.total_scenarios or done < run.total_scenarios:
        return None
    if _provenance_mismatch(run):
        if AuditRun.objects.filter(pk=run.pk, status__in=_ACTIVE_STATUSES).update(
            status=AuditRun.Status.FAILED, error_code="SIMPLEAUDIT_VERSION_MISMATCH", finished_at=timezone.now(),
        ):
            append_event(run_id, "_run", "run_failed", {"code": "SIMPLEAUDIT_VERSION_MISMATCH"})
        return "failed"
    from audits.events import ScenarioResult

    if not ScenarioResult.objects.filter(run_id=run.pk, status="completed").exists():
        # Every scenario failed (usually a wrong URL, model id or key): the run failed.
        first = ScenarioResult.objects.filter(run_id=run.pk).order_by("id").values_list("result", flat=True).first() or {}
        error = str((first.get("judgment") or {}).get("error") or first.get("error") or first.get("summary") or "")
        if AuditRun.objects.filter(pk=run.pk, status__in=_ACTIVE_STATUSES).update(
            status=AuditRun.Status.FAILED, error_code="ALL_SCENARIOS_FAILED", completed_scenarios=done,
            error_message=f"Every scenario failed. First error: {error}"[:2000], finished_at=timezone.now(),
        ):
            append_event(run_id, "_run", "run_failed", {"code": "ALL_SCENARIOS_FAILED", "error": error})
        return "failed"
    if AuditRun.objects.filter(pk=run.pk, status__in=_ACTIVE_STATUSES).update(
        status=AuditRun.Status.COMPLETED, finished_at=timezone.now(), completed_scenarios=done,
    ):
        append_event(run_id, "_run", "run_stage", {"stage": "aggregation"})
        append_event(run_id, "_run", "run_completed", {"scenarios": done})
    return "completed"


def _missing_version_item_ids(run) -> list[str]:
    """Pinned scenarios of ``run`` without a final result yet."""
    from scenarios.models import ScenarioSetVersionItem

    done = set(_terminal_results(run.pk).values_list("version_item_id", flat=True))
    return [
        str(pk)
        for pk in ScenarioSetVersionItem.objects.filter(version=run.scenario_set_version)
        .order_by("position").values_list("pk", flat=True)
        if str(pk) not in done
    ]


def resume_run(run, reason: str) -> int:
    """Resubmit the run's missing scenarios; finalize if none are missing.

    Safe to call at any time: already-final scenarios are not resubmitted, a
    scenario still queued or running rejects its duplicate (per-scenario
    concurrency key), and the durable attempt budget bounds retries. Returns
    how many scenarios were resubmitted.
    """
    from audits.events import append_event
    from audits.models import AuditRun

    missing = _missing_version_item_ids(run)
    if not missing:
        _maybe_finalize(str(run.pk))
        return 0
    submit_run_workflow(
        str(run.pk), missing, simpleaudit_version=run.simpleaudit_version, git_commit=run.git_commit,
    )
    run.refresh_from_db()
    meta = dict(run.runtime_metadata or {})
    meta["submission"] = {"status": "submitted", "at": timezone.now().isoformat(), "scenarios": len(missing)}
    meta["resumes"] = int(meta.get("resumes") or 0) + 1
    meta["last_resume"] = {"at": timezone.now().isoformat(), "reason": reason, "scenarios": len(missing)}
    AuditRun.objects.filter(pk=run.pk).update(runtime_metadata=meta)
    append_event(run.pk, "_run", "run_resumed", {"reason": reason, "scenarios": len(missing)})
    logger.info("Resumed run %s (%s): resubmitted %d scenario(s)", run.pk, reason, len(missing))
    return len(missing)


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
        return {"status": "cancelled"}

    try:
        current = AuditRun.objects.get(pk=int(run_id))
    except (AuditRun.DoesNotExist, ValueError):
        return {"status": "missing"}
    status = _maybe_finalize(run_id)
    done = _terminal_results(run_id).count()
    if status is None:
        # Not done yet. No blocking wait: the last scenario finalizes the run
        # inline and the sweeper is the backstop, so this never holds a slot.
        total = workflow_input.total_scenarios or current.total_scenarios or 0
        append_event(run_id, "_run", "finalize_waiting", {"done": done, "total": total})
        raise RuntimeError(f"finalize premature: {done}/{total} scenarios have results")
    return {"status": status, "scenarios": done}


@retry_if_locked
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


# --- Module-level task registration ---------------------------------------
# Tasks are registered against the shared client. This is required for the
# single-process (minimal-config) deployment: the web server thread submits runs
# via `submit_run_workflow`, which needs the same task objects the worker
# subscribes to.
#
# The client can change AFTER import — most notably in `dev_server --embedded`,
# where the embedded Hatchet engine is started *after* this module is imported
# (so the initial registration binds to the external `hatchet-server` config).
# `_ensure_tasks_registered` therefore re-registers both tasks whenever the
# active client differs from the one they were built against, so submission and
# crash recovery always target the latest available Hatchet.
_scenario_task = None
_finalize_task = None
_tasks_client_id: int | None = None


def _register_tasks(client: Hatchet) -> None:
    """Register (or re-register) both tasks against ``client``."""
    global _scenario_task, _finalize_task, _tasks_client_id
    # Scenario retries on transient failures; finalize retries because it may be
    # scheduled before slow scenarios finish (_run_finalize_impl raises until
    # every pinned scenario has a durable result). backoff_factor=2.0 with 10
    # retries spans several minutes, covering typical scenario completion times.
    from hatchet_sdk.types.concurrency import (
        ConcurrencyExpression,
        ConcurrencyLimitStrategy,
    )

    _scenario_task = client.task(
        name="audit.scenario_execute",
        input_validator=ScenarioInput,
        retries=SCENARIO_RETRIES,
        backoff_factor=2.0,
        # At most one live execution per (run, scenario): a resubmission while
        # the original is still queued or running is dropped, so crash
        # recovery and resume can never run a scenario twice at once.
        concurrency=ConcurrencyExpression(
            expression="input.run_id + ':' + input.version_item_id",
            max_runs=1,
            limit_strategy=ConcurrencyLimitStrategy.CANCEL_NEWEST,
        ),
        # Effectively no cap (Hatchet requires a value). A scenario runs in a
        # Python thread that cannot be killed, so a timeout would not stop it:
        # Hatchet would start a duplicate retry beside the still-running
        # original, doubling model calls and starving worker slots. Hangs are
        # handled by per-request HTTP timeouts, engine retries, and the
        # stuck-run sweeper instead.
        execution_timeout=timedelta(days=365),
    )(with_fresh_connection(_scenario_execute_impl))
    # Legacy: no longer submitted (runs finalize inline when their last scenario
    # lands, with the sweeper as backstop). Kept registered so finalize tasks
    # already queued in Hatchet from older submissions still resolve.
    _finalize_task = client.task(
        name="audit.run_finalize",
        input_validator=FinalizeInput,
        execution_timeout="60s",
        retries=0,
    )(with_fresh_connection(_run_finalize_impl))
    _tasks_client_id = id(client)


def _ensure_tasks_registered() -> None:
    """Ensure tasks are registered against the *current* shared client.

    Re-registers if the active client changed since the last registration (e.g.
    the embedded engine was started after import), or if not yet registered.
    """
    client = get_client()
    if _scenario_task is None or _tasks_client_id != id(client):
        _register_tasks(client)


_ensure_tasks_registered()


def submit_run_workflow(run_id: str, version_item_ids: list[str], *, simpleaudit_version: str | None, git_commit: str | None):
    """Enqueue one ``audit.scenario_execute`` task per given scenario.

    Standalone tasks are the reliable primitive: the worker subscribes to a
    fixed set of action names at startup. There is no separate finalize task:
    the scenario that writes the last final result completes the run inline
    (``_maybe_finalize``), and the sweeper completes or resumes anything left.

    Safe to call repeatedly for the same scenarios (see ``resume_run``).
    Returns the last task run reference, or raises if the client is unavailable.
    """
    # Re-register tasks against the current client if it changed since import
    # (e.g. embedded engine started after this module loaded). Without this,
    # submission would target a stale client and fail with DNS/RPC errors.
    _ensure_tasks_registered()
    ref = None
    for vid in version_item_ids:
        ref = _scenario_task.run(
            input=ScenarioInput(run_id=str(run_id), version_item_id=str(vid)),
            wait_for_result=False,
        )
    return ref


def build_worker() -> Worker:
    """Build the Hatchet worker bound to the configured pool label.

    Subscribes to the two module-level task handlers (``audit.scenario_execute``
    and ``audit.run_finalize``). These are the only actions the worker subscribes
    to, and they are exactly what ``submit_run_workflow`` enqueues for each audit
    run (one scenario task per pinned scenario plus a finalize task). Keeping the
    action set fixed at startup is what makes dispatch reliable — see the note in
    ``submit_run_workflow`` about why dynamic per-run workflows are not used.
    """
    client = get_client()
    # Ensure the task objects reflect this client (re-register if it changed).
    _ensure_tasks_registered()
    return Worker(
        name=f"simpleaudit-audit-worker-{settings.WORKER_POOL}",
        config=client.config,
        slot_config={"default": 4},
        labels={"pool": settings.WORKER_POOL},
        workflows=[_scenario_task, _finalize_task],
    )


def _recover_stuck_runs() -> None:
    """Re-submit audit runs that were left in-flight when the worker died.

    Called once at worker startup, before the task loop begins. Finds runs in a
    non-terminal state (queued through report_generation) whose scenario tasks
    are no longer active in Hatchet (because the previous worker process was
    killed) and re-enqueues them. This is safe because scenario tasks are
    idempotent: already-completed scenarios skip instantly via the durable
    result check.

    Only the missing scenarios are resubmitted (see ``resume_run``).
    """
    from audits.models import AuditRun

    # No grace period: resuming is idempotent (per-scenario concurrency key +
    # durable result checks), so it cannot double-run work a healthy worker is
    # doing, and a run the crash interrupted resumes immediately.
    stuck = AuditRun.objects.filter(status__in=_ACTIVE_STATUSES, archived=False)

    recovered = 0
    for run in stuck:
        try:
            if resume_run(run, reason="worker_start"):
                recovered += 1
        except Exception as exc:  # noqa: BLE001 - crash recovery must not fail the whole sweep
            logger.warning("Crash recovery: failed to resume run %s: %s", run.pk, exc)

    if recovered:
        logger.info("Crash recovery: re-submitted %d stuck run(s)", recovered)


def _sweep_once(stale_minutes: int = RESUME_STALE_MINUTES) -> None:
    """One sweeper pass over active runs: complete, submit, or resume them.

    - every scenario has a final result -> complete the run
    - submission never went through (marked pending, or no workflow id and no
      activity after ``NEVER_SUBMITTED_GRACE_SECONDS``) -> submit it
    - no progress events for ``stale_minutes`` -> resume (resubmit missing)

    Runs are never failed for being slow: a run queued behind other runs, or
    waiting for the worker to come back, is resumed, not killed. Scenarios that
    keep crashing are bounded by the durable attempt budget instead.
    """
    from datetime import timedelta as _td

    from audits.events import AuditEvent
    from audits.models import AuditRun

    cutoff = timezone.now() - _td(minutes=stale_minutes)
    for run in AuditRun.objects.filter(status__in=_ACTIVE_STATUSES, archived=False):
        try:
            if _maybe_finalize(str(run.pk)):
                continue
            meta = run.runtime_metadata or {}
            if (meta.get("submission") or {}).get("status") == "pending":
                resume_run(run, reason="submission_retry")
                continue
            last_event = AuditEvent.objects.filter(run_id=run.pk).order_by("-id").only("created_at").first()
            last_activity = last_event.created_at if last_event else (run.queued_at or run.created_at)
            never_submitted = not run.workflow_run_id and last_event is None
            if never_submitted and last_activity < timezone.now() - _td(seconds=NEVER_SUBMITTED_GRACE_SECONDS):
                resume_run(run, reason="never_submitted")
            elif last_activity and last_activity < cutoff:
                resume_run(run, reason="stalled")
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the sweep
            logger.warning("Sweeper: run %s skipped: %s", run.pk, exc)


def _stuck_run_sweeper(interval: int = 60, stale_minutes: int = RESUME_STALE_MINUTES) -> None:
    """Background loop around ``_sweep_once`` and the monitor tick; never crashes."""
    import time

    logger.info("Run sweeper started (interval=%ds, resume after %dmin idle)", interval, stale_minutes)
    from django.db import close_old_connections

    while True:
        time.sleep(interval)
        close_old_connections()   # this thread lives forever; recycle dead or aged connections
        try:
            _sweep_once(stale_minutes)
        except Exception as exc:  # noqa: BLE001 - sweeper must never crash
            logger.warning("Run sweeper iteration failed: %s", exc)
        try:
            from audits.monitors import run_due_monitors

            run_due_monitors()
        except Exception as exc:  # noqa: BLE001 - sweeper must never crash
            logger.warning("Monitor tick failed: %s", exc)


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

    # Periodic sweeper: completes finished runs, resumes stalled ones.
    _sweeper = _threading.Thread(target=_stuck_run_sweeper, daemon=True, name="stuck-run-sweeper")
    _sweeper.start()

    print(f"Starting SimpleAudit worker (pool={settings.WORKER_POOL})...", flush=True)
    worker.start()
