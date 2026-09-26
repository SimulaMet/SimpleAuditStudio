"""Audit run creation and freeze services."""
import logging

from django.db import transaction
from django.utils import timezone

from accounts.models import Project
from audits.events import append_event
from audits.models import AuditRun
from infra.exceptions import StableAPIError
from infra.simpleaudit_package import resolve_engine_provenance
from model_registry.models import RegisteredModel
from scenarios.models import ScenarioSetVersion
from scenarios.services import require_project_role

logger = logging.getLogger("simpleaudit.audit")


def _endpoint_snapshot(model: RegisteredModel) -> dict:
    """Build the frozen config snapshot from a RegisteredModel + its connection.

    The JSON shape is byte-compatible with what infra/engine.py expects
    (base_url, provider, model_id, secret_reference, api_key_direct, ...).
    """
    conn = model.connection
    return {
        "id": model.id,
        "display_name": model.display_name,
        "provider": conn.provider,
        "base_url": conn.base_url,
        "model_id": model.model_id,
        "model_revision": model.model_revision,
        "capabilities": model.capabilities,
        "default_parameters": model.default_parameters,
        "secret_reference": conn.secret_reference,
        "api_key_direct": conn.api_key_direct,
        "enabled": model.enabled and conn.enabled,
    }


def _generation_parameters(
    *,
    max_turns_override: int | None = None,
    language_override: str | None = None,
    n_repetitions_override: int | None = None,
    gen_config_override: dict | None = None,
) -> dict:
    params = {}
    # Explicit form overrides are captured in the frozen manifest.
    if max_turns_override is not None:
        params["max_turns"] = max_turns_override
    if language_override:
        params["language"] = language_override
    if n_repetitions_override is not None:
        params["n_repetitions"] = n_repetitions_override
    # Raw JSON override merges last (highest priority).
    if gen_config_override:
        params.update(gen_config_override)
    return params


@transaction.atomic
def create_audit_run(
    *,
    project: Project,
    user,
    name: str,
    scenario_set_version: ScenarioSetVersion,
    target_model: RegisteredModel,
    auditor_model: RegisteredModel,
    judge_model: RegisteredModel,
    max_turns_override: int | None = None,
    language_override: str | None = None,
    n_repetitions_override: int | None = None,
    gen_config_override: dict | None = None,
) -> AuditRun:
    """Create a queued AuditRun with immutable execution inputs.

    This does not enqueue work yet. The durable job system integration will add
    workflow submission after the Phase 4 spike validates the selected system.
    """
    require_project_role(user, project)
    if scenario_set_version.scenario_set.project_id != project.id:
        raise StableAPIError(detail="Scenario set version belongs to another project.", code="cross_project_input")
    for model in (target_model, auditor_model, judge_model):
        if model.project_id != project.id or not model.enabled or not model.connection.enabled:
            raise StableAPIError(detail="Model is unavailable in this project.", code="model_unavailable")
    # Provenance is authoritative: it comes from the installed SimpleAudit
    # package metadata (version) and its PEP 610 direct_url commit (optional).
    # Callers cannot supply their own — that would let a manifest claim an engine
    # the worker does not actually have. The version is required; the commit is
    # optional (registry installs have none).
    provenance = resolve_engine_provenance()
    resolved_version = provenance.version or ""
    resolved_commit = provenance.commit or ""
    if not resolved_version:
        raise StableAPIError(
            detail="SimpleAudit engine is not installed; cannot create an audit run without engine provenance.",
            code="simpleaudit_provenance_required",
        )

    now = timezone.now()
    return AuditRun.objects.create(
        project=project,
        name=name.strip(),
        status=AuditRun.Status.QUEUED,
        scenario_set_version=scenario_set_version,
        target_model=target_model,
        auditor_model=auditor_model,
        judge_model=judge_model,
        target_config_snapshot=_endpoint_snapshot(target_model),
        auditor_config_snapshot=_endpoint_snapshot(auditor_model),
        judge_config_snapshot=_endpoint_snapshot(judge_model),
        generation_parameters_snapshot=_generation_parameters(
            max_turns_override=max_turns_override,
            language_override=language_override,
            n_repetitions_override=n_repetitions_override,
            gen_config_override=gen_config_override,
        ),
        simpleaudit_version=resolved_version,
        git_commit=resolved_commit,
        runtime_metadata={"created_by_username": user.username},
        queued_at=now,
        total_scenarios=scenario_set_version.scenario_count,
        created_by=user,
    )


def submit_audit_run(run: AuditRun) -> str | None:
    """Enqueue durable work for a frozen AuditRun into Hatchet.

    Returns the Hatchet workflow run id on success, or ``None`` if submission was
    not possible (e.g. no live server / token). Submission is deliberately kept
    OUTSIDE the freeze transaction: a downed job system must not roll back an
    already-frozen experiment record. On failure the run stays ``queued`` and the
    reason is recorded in ``runtime_metadata["submission"]`` so an operator (or a
    later retry command) can resubmit without re-freezing inputs.
    """
    from infra.worker import (
        submit_run_workflow,  # lazy: needs no live server at import time
    )

    items = list(
        run.scenario_set_version.items.select_related("scenario", "revision").order_by("position")
    )
    version_item_ids = [str(item.id) for item in items]

    try:
        ref = submit_run_workflow(
            run.id,
            version_item_ids,
            simpleaudit_version=run.simpleaudit_version,
            git_commit=run.git_commit,
        )
    except Exception as exc:  # noqa: BLE001 - any client/config error means "not submittable now"
        _record_submission_failure(run, f"submit_failed: {exc}")
        logger.warning("Audit %s not submitted: Hatchet unavailable (%s)", run.id, exc)
        return None

    # The per-run workflow has one step per scenario plus a dependent finalize
    # step, so the run cannot be marked completed until every scenario has a
    # result. wait_for_result=False enqueues and returns immediately; the worker
    # executes the steps asynchronously.
    append_event(run.id, "_run", "run_queued", {"scenarios": len(items)})
    append_event(run.id, "_run", "run_stage", {"stage": "preparing"})
    run.refresh_from_db()
    meta = dict(run.runtime_metadata or {})
    meta["submission"] = {
        "status": "submitted",
        "at": timezone.now().isoformat(),
        "scenarios": len(items),
        "workflow_run": getattr(ref, "id", None) or str(ref),
    }
    AuditRun.objects.filter(id=run.id).update(runtime_metadata=meta)
    logger.info("Audit %s submitted to Hatchet with %d scenarios", run.id, len(items))
    return None


def _record_submission_failure(run: AuditRun, reason: str) -> None:
    run.refresh_from_db()
    meta = dict(run.runtime_metadata or {})
    meta["submission"] = {"status": "pending", "reason": reason, "at": timezone.now().isoformat()}
    AuditRun.objects.filter(id=run.id).update(runtime_metadata=meta)
