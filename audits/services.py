"""Audit run creation and freeze services."""
import logging

from django.db import transaction
from django.utils import timezone

from accounts.models import Project, ProjectMembership
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

    Never contains a raw API key (snapshots are visible to every workspace
    member through the runs API). The worker resolves the key at execution time
    from ``connection_id`` or ``secret_reference`` (``infra.engine.snapshot_api_key``).
    """
    conn = model.connection
    return {
        "id": model.id,
        "connection_id": conn.id,
        "display_name": model.display_name,
        "provider": conn.provider,
        "base_url": conn.base_url,
        "model_id": model.model_id,
        "model_revision": model.model_revision,
        "capabilities": model.capabilities,
        "default_parameters": model.default_parameters,
        "secret_reference": conn.secret_reference,
        "enabled": model.enabled and conn.enabled,
    }


ROLES = ("target", "auditor", "judge")


def frozen_model(run: AuditRun, role: str) -> dict:
    """The model a run actually used for ``role``, from its frozen snapshot.

    Models and connections can be renamed or re-pointed after a run, so pages
    show the snapshot, not the live record. ``changes`` lists what differs in
    the live record now (for an "edited since this run" note).
    """
    snap = getattr(run, f"{role}_config_snapshot") or {}
    live = getattr(run, f"{role}_model", None)
    view = {
        "role": role,
        "display_name": snap.get("display_name") or snap.get("model_id") or (live.display_name if live else "—"),
        "model_id": snap.get("model_id") or (live.model_id if live else ""),
        "base_url": snap.get("base_url", ""),
        "provider": snap.get("provider", ""),
        "connection_id": snap.get("connection_id") or (live.connection_id if live else None),
        "changes": [],
    }
    if live is not None and snap:
        conn = live.connection
        for label, then, now in (
            ("name", view["display_name"], live.display_name),
            ("model ID", view["model_id"], live.model_id),
            ("base URL", view["base_url"], conn.base_url),
            ("provider", view["provider"], conn.provider),
        ):
            if then and now and then != now:
                view["changes"].append(f"{label} is now “{now}”")
    return view


def frozen_name(run: AuditRun, role: str, *, with_id: bool = False) -> str:
    """Display name (optionally "Name (model-id)") of the model the run used, from its snapshot."""
    m = frozen_model(run, role) if with_id else None
    if m:
        return f"{m['display_name']} ({m['model_id']})" if m["model_id"] and m["model_id"] != m["display_name"] else m["display_name"]
    snap = getattr(run, f"{role}_config_snapshot") or {}
    live = getattr(run, f"{role}_model", None)
    return snap.get("display_name") or snap.get("model_id") or (live.display_name if live else "—")


def frozen_judge(run: AuditRun) -> dict:
    """The judge a run was graded with (name, version, base, texts), from its snapshot."""
    grading = (run.judge_config_snapshot or {}).get("judge") or {}
    return {
        "judge_id": grading.get("judge_id"),
        "name": grading.get("name") or "Judge",
        "version": grading.get("version"),
        "label": f"{grading.get('name') or 'Judge'} v{grading.get('version') or '?'}",
        "base_name": grading.get("base_name", ""),
        "output": grading.get("output") or "severity",
        "output_label": grading.get("output_label") or "Severity",
        "criteria": grading.get("criteria", ""),
        "judge_prompt": grading.get("judge_prompt", ""),
        "probe_prompt": grading.get("probe_prompt", ""),
        "custom_criteria": grading.get("custom_criteria", False),
        "custom_probe_prompt": grading.get("custom_probe_prompt", False),
        "model": frozen_name(run, "judge"),
    }


def frozen_agent(run: AuditRun) -> dict | None:
    """The Agent a run targeted, from its frozen snapshot.

    Returns ``None`` when the run has no agent (bare-model target).
    """
    snap = run.agent_config_snapshot
    if not snap:
        return None
    return {
        "id": snap.get("agent_id"),
        "name": snap.get("name", "—"),
        "base_model": snap.get("base_model", {}),
        "system_prompt": snap.get("system_prompt", ""),
        "knowledge_bases": snap.get("knowledge_bases", []),
        "tools": snap.get("tools", []),
        "retrieval": snap.get("retrieval") or snap.get("retrieval_profile"),
        "server_rag": snap.get("server_rag"),
        "capabilities": snap.get("capabilities", []),
        "metadata": snap.get("metadata", {}),
    }


# Keys managed by dedicated form fields — stripped from JSON override to avoid
# confusion. Probe / judge prompts belong to the judge, not the run.
_FORM_MANAGED_KEYS = {"max_turns", "n_repetitions", "language", "probe_prompt", "judge_prompt"}


def _generation_parameters(
    *,
    max_turns_override: int | None = None,
    language_override: str | None = None,
    n_repetitions_override: int | None = None,
    gen_config_override: dict | None = None,
) -> dict:
    params = {}
    # Raw JSON override provides the base (e.g. cloned generation config),
    # but strip keys that are managed by dedicated form fields to prevent
    # silent conflicts and user confusion.
    if gen_config_override:
        for k, v in gen_config_override.items():
            if k not in _FORM_MANAGED_KEYS:
                params[k] = v
    # Explicit form fields are authoritative.
    if max_turns_override is not None:
        params["max_turns"] = max_turns_override
    if language_override:
        params["language"] = language_override
    if n_repetitions_override is not None:
        params["n_repetitions"] = n_repetitions_override
    return params


def frozen_inputs(
    *,
    target_model: RegisteredModel | None,
    auditor_model: RegisteredModel,
    judge_model: RegisteredModel,
    judge,
    max_turns_override: int | None = None,
    language_override: str | None = None,
    n_repetitions_override: int | None = None,
    gen_config_override: dict | None = None,
) -> dict:
    """The snapshots a run freezes: the three endpoints, the judge and the
    generation settings. Shared by ``create_audit_run`` and script generation
    for runs not created yet (infra.codegen)."""
    from judges.services import judge_snapshot

    return {
        "target_config_snapshot": _endpoint_snapshot(target_model),
        "auditor_config_snapshot": _endpoint_snapshot(auditor_model),
        "judge_config_snapshot": {**_endpoint_snapshot(judge_model), "judge": judge_snapshot(judge)},
        "generation_parameters_snapshot": _generation_parameters(
            max_turns_override=max_turns_override,
            language_override=language_override,
            n_repetitions_override=n_repetitions_override,
            gen_config_override=gen_config_override,
        ),
    }


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
    judge,
    max_turns_override: int | None = None,
    language_override: str | None = None,
    n_repetitions_override: int | None = None,
    gen_config_override: dict | None = None,
    trace_config: dict | None = None,
    monitor=None,
    experiment=None,
    agent=None,
) -> AuditRun:
    """Create a queued AuditRun with immutable execution inputs.

    This does not enqueue work yet. The durable job system integration will add
    workflow submission after the Phase 4 spike validates the selected system.

    ``judge`` is a ``JudgeVersion`` (criteria, output format, probe prompt); ``judge_model`` grades with it.

    When ``agent`` is provided, the run targets the Agent's Open WebUI wrapper;
    its base model is only part of the frozen configuration. The agent's full configuration is frozen in
    ``agent_config_snapshot`` for reproducibility.
    """
    # Launching spends the workspace's API keys: viewers may not.
    require_project_role(user, project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    if scenario_set_version.scenario_set.project_id != project.id:
        raise StableAPIError(detail="Scenario set version belongs to another project.", code="cross_project_input")
    if judge.judge.project_id != project.id:
        raise StableAPIError(detail="Judge belongs to another project.", code="cross_project_input")
    if agent is not None:
        if agent.project_id != project.id:
            raise StableAPIError(detail="Agent belongs to another project.", code="cross_project_input")
        if not agent.enabled:
            raise StableAPIError(detail="Agent is disabled.", code="agent_disabled")
        from model_registry.services import agent_target_model

        try:
            target_model = agent_target_model(agent)
        except ValueError as exc:
            raise StableAPIError(detail=str(exc), code="agent_not_synced") from exc
    if target_model is None:
        raise StableAPIError(detail="A target model is required.", code="target_model_required")
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

    # Default tracing: when the target's connection has an OTLP credential (the
    # target is configured to export its spans to Studio), capture them
    # automatically in studio mode. The worker resolves the credential's
    # target_id at execution time; an explicit trace_config from the caller wins.
    if trace_config is None:
        from model_registry.models import OTLPCredential

        target_id = (
            OTLPCredential.objects.filter(connection_id=target_model.connection_id, enabled=True)
            .order_by("-created_at")
            .values_list("target_id", flat=True)
            .first()
        )
        trace_config = {"mode": "studio", "target_id": target_id or ""} if target_id else {}

    now = timezone.now()
    agent_snapshot = None
    if agent is not None:
        server_rag = None
        tool_invocation_names = {}
        try:
            from integrations.openwebui.client import OpenWebUIAdapter

            adapter = OpenWebUIAdapter.for_admin()
        except Exception:  # noqa: BLE001 - a down chat service must not block freezing local inputs
            adapter = None
            logger.warning("Could not connect to Open WebUI while freezing agent %s", agent.id)
        if adapter is not None:
            try:
                server_rag = adapter.safe_rag_settings()
            except Exception:  # noqa: BLE001 - a down chat service must not block freezing local inputs
                logger.warning("Could not freeze Open WebUI RAG settings for agent %s", agent.id)
            try:
                tool_invocation_names = adapter.tool_invocation_names(
                    list(agent.tools.exclude(external_id="").values_list("external_id", flat=True))
                )
            except Exception:  # noqa: BLE001 - tool metadata is optional evidence
                logger.warning("Could not freeze Open WebUI tool names for agent %s", agent.id)
        agent_snapshot = agent.config_snapshot(
            server_rag=server_rag, tool_invocation_names=tool_invocation_names
        )
    return AuditRun.objects.create(
        project=project,
        name=name.strip(),
        status=AuditRun.Status.QUEUED,
        scenario_set_version=scenario_set_version,
        target_model=target_model,
        agent=agent,
        auditor_model=auditor_model,
        judge_version=judge,
        judge_model=judge_model,
        **frozen_inputs(
            target_model=target_model, auditor_model=auditor_model, judge_model=judge_model, judge=judge,
            max_turns_override=max_turns_override, language_override=language_override,
            n_repetitions_override=n_repetitions_override, gen_config_override=gen_config_override,
        ),
        agent_config_snapshot=agent_snapshot,
        simpleaudit_version=resolved_version,
        git_commit=resolved_commit,
        trace_config=trace_config or {},
        runtime_metadata={"created_by_username": user.username},
        queued_at=now,
        total_scenarios=scenario_set_version.scenario_count,
        created_by=user,
        monitor=monitor,
        experiment=experiment,
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

    # Written BEFORE submission: the worker can start scenarios (and log
    # target_execution) before submit returns, and a later "preparing" would
    # make the live progress stage appear to go backwards.
    append_event(run.id, "_run", "run_queued", {"scenarios": len(items)})
    append_event(run.id, "_run", "run_stage", {"stage": "preparing"})

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


def cancel_run(run: AuditRun, user) -> bool:
    """Cancel a queued or running run; False when it had already finished.

    Writes a terminal event so live progress ends right away; running scenarios
    see the durable flag and stop at their next repetition.
    """
    from audits.events import append_event

    if not run.is_active:
        return False
    run.status = AuditRun.Status.CANCELLED
    run.finished_at = run.finished_at or timezone.now()
    run.save(update_fields=["status", "finished_at"])
    append_event(run.pk, "_run", "run_cancelled", {"by": user.username})
    return True
