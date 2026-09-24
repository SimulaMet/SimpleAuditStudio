"""Seed realistic demo audit runs using the SimulaChat endpoint.

Creates a SimulaChat model connection, then runs small audits (a subset of
scenarios from existing packs) to populate the dashboard with real data.

Usage:
    python manage.py shell < deploy/seed_demo_audits.py

Or as a standalone script (requires DJANGO_SETTINGS_MODULE):
    python deploy/seed_demo_audits.py

Idempotent: skips if demo runs already exist for the project.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import timedelta

# Allow running as a standalone script or from /app in the container
if __name__ == "__main__":
    if "DJANGO_SETTINGS_MODULE" not in os.environ:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Ensure project root is on path (works from repo root or /app)
    _root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if _root not in sys.path:
        sys.path.insert(0, _root)

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

logger = logging.getLogger("simpleaudit.seed_demo")

SIMULACHAT_BASE_URL = "https://simulachat.sushant.pp.ua/api/v1"
SIMULACHAT_API_KEY = os.environ.get("SIMULACHAT_API_KEY", "sk-3582392995f54374a6574414a37cd7c5")

# Models to use for the demo audit roles
TARGET_MODEL_ID = "Qwen3.8-27B"
AUDITOR_MODEL_ID = "glm-5-2-fp8"
JUDGE_MODEL_ID = "glm-5-2-fp8"

# How many scenarios to run per pack (keep it small for speed)
SCENARIOS_PER_PACK = 3


def ensure_simulachat_connection(project, user):
    """Create SimulaChat ModelConnection + ModelEndpoints if missing."""
    from model_registry.models import ModelConnection, ModelEndpoint, RegisteredModel

    conn, created = ModelConnection.objects.get_or_create(
        project=project,
        name="SimulaChat",
        defaults={
            "provider": "openai",
            "base_url": SIMULACHAT_BASE_URL,
            "api_key_direct": SIMULACHAT_API_KEY,
            "enabled": True,
            "created_by": user,
        },
    )
    if created:
        logger.info("Created SimulaChat model connection")

    endpoints = {}
    for display_name, model_id in [
        ("Qwen3.8-27B", TARGET_MODEL_ID),
        ("GLM-5.2 FP8", AUDITOR_MODEL_ID),
    ]:
        ep, ep_created = ModelEndpoint.objects.get_or_create(
            project=project,
            display_name=display_name,
            defaults={
                "provider": "openai",
                "base_url": SIMULACHAT_BASE_URL,
                "model_id": model_id,
                "enabled": True,
                "default_parameters": {"temperature": 0.7, "max_tokens": 4096},
                "api_key_direct": SIMULACHAT_API_KEY,
                "created_by": user,
            },
        )
        endpoints[model_id] = ep
        if ep_created:
            logger.info("  + %s (%s)", display_name, model_id)

        RegisteredModel.objects.get_or_create(
            connection=conn,
            project=project,
            model_id=model_id,
            defaults={
                "display_name": display_name,
                "enabled": True,
                "default_parameters": {"temperature": 0.7, "max_tokens": 4096},
            },
        )

    return endpoints


def get_scenario_set_version(project, pack_name: str):
    """Find an existing published ScenarioSetVersion for a pack."""
    from scenarios.models import ScenarioSet, ScenarioSetVersion

    set_obj = ScenarioSet.objects.filter(project=project, name=f"SimpleAudit: {pack_name}").first()
    if not set_obj:
        return None
    version = set_obj.versions.order_by("-version").first()
    if not version or not version.items.exists():
        return None
    return version


def run_demo_audit(project, user, pack_name: str, endpoints: dict) -> bool:
    """Create and execute a small audit run for one pack. Returns True on success."""
    from audits.events import append_event, upsert_scenario_result
    from audits.models import AuditRun
    from infra.engine import EngineError, run_scenario
    from infra.simpleaudit_package import resolve_engine_provenance
    from scenarios.models import ScenarioSetVersionItem

    version = get_scenario_set_version(project, pack_name)
    if not version:
        logger.warning("No scenario set version found for pack '%s' — skipping.", pack_name)
        return False

    items = list(version.items.select_related("scenario", "revision").order_by("position"))[:SCENARIOS_PER_PACK]
    if not items:
        logger.warning("Pack '%s' has no scenarios — skipping.", pack_name)
        return False

    provenance = resolve_engine_provenance()
    target_ep = endpoints[TARGET_MODEL_ID]
    auditor_ep = endpoints[AUDITOR_MODEL_ID]
    judge_ep = endpoints[JUDGE_MODEL_ID]

    def _snap(ep):
        return {
            "id": ep.id,
            "display_name": ep.display_name,
            "provider": ep.provider,
            "base_url": ep.base_url,
            "model_id": ep.model_id,
            "model_revision": ep.model_revision,
            "capabilities": ep.capabilities,
            "default_parameters": ep.default_parameters,
            "secret_reference": ep.secret_reference,
            "api_key_direct": ep.api_key_direct,
            "enabled": ep.enabled,
        }

    now = timezone.now()
    run = AuditRun.objects.create(
        project=project,
        name=f"Demo: {pack_name} safety check",
        status=AuditRun.Status.PREPARING,
        scenario_set_version=version,
        target_endpoint=target_ep,
        auditor_endpoint=auditor_ep,
        judge_endpoint=judge_ep,
        target_config_snapshot=_snap(target_ep),
        auditor_config_snapshot=_snap(auditor_ep),
        judge_config_snapshot=_snap(judge_ep),
        generation_parameters_snapshot={"max_turns": 3, "language": "English"},
        simpleaudit_version=provenance.version or "unknown",
        git_commit=provenance.commit or "",
        runtime_metadata={"created_by_username": user.username, "demo_seed": True},
        queued_at=now - timedelta(minutes=5),
        started_at=now,
        total_scenarios=len(items),
        created_by=user,
    )

    logger.info("Created demo run %d: %s (%d scenarios)", run.id, run.name, len(items))
    append_event(run.id, "_run", "run_queued", {"scenarios": len(items)})
    append_event(run.id, "_run", "run_stage", {"stage": "preparing"})

    completed = 0
    successful = 0
    failed = 0

    for item in items:
        vid = str(item.id)
        revision = item.revision
        scenario = item.scenario

        append_event(run.id, vid, "scenario_attempted", {"attempt": 1})
        append_event(run.id, "_run", "run_stage", {"stage": "target_execution"})

        try:
            result = run_scenario(
                name=scenario.key,
                description=revision.description,
                expected_behavior=revision.expected_behavior or None,
                test_prompt=revision.test_prompt or None,
                target=run.target_config_snapshot,
                auditor=run.auditor_config_snapshot,
                judge=run.judge_config_snapshot,
                generation={"max_turns": 3, "language": "English"},
            )
            severity = result.get("severity", "")
            is_error = severity.upper() == "ERROR"
            status = "failed" if is_error else "completed"
        except EngineError as exc:
            logger.error("Scenario %s failed: %s", scenario.key, exc)
            result = {"error": str(exc)}
            severity = ""
            status = "failed"
            is_error = True

        upsert_scenario_result(run.id, vid, status=status, attempts=1, result=result)
        completed += 1
        if is_error:
            failed += 1
        else:
            successful += 1

        append_event(
            run.id, vid,
            "scenario_completed" if not is_error else "scenario_failed",
            {"attempt": 1, "severity": severity},
        )
        logger.info("  [%d/%d] %s → %s (%s)", completed, len(items), scenario.title, status, severity)

    # Finalize
    run.completed_scenarios = completed
    run.successful_scenarios = successful
    run.failed_scenarios = failed
    run.status = AuditRun.Status.COMPLETED if failed == 0 else AuditRun.Status.COMPLETED
    run.finished_at = timezone.now()

    # Compute summary metrics
    severities = []
    for item in items:
        from audits.events import get_result
        r = get_result(run.id, str(item.id))
        if r and r["result"]:
            sev = r["result"].get("severity", "")
            if sev:
                severities.append(sev)

    run.summary_metrics = {
        "total": completed,
        "passed": successful,
        "failed": failed,
        "pass_rate": round(successful / max(completed, 1), 3),
        "severity_distribution": _count_severities(severities),
    }
    run.save(update_fields=[
        "status", "finished_at", "completed_scenarios",
        "successful_scenarios", "failed_scenarios", "summary_metrics",
    ])

    append_event(run.id, "_run", "run_stage", {"stage": "aggregation"})
    append_event(run.id, "_run", "run_completed", {"scenarios": completed})
    logger.info("Demo run %d completed: %d/%d passed", run.id, successful, completed)
    return True


def _count_severities(severities: list[str]) -> dict:
    counts: dict[str, int] = {}
    for s in severities:
        key = s.upper() if s else "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    return counts


def main():
    from accounts.models import Project
    from django.contrib.auth import get_user_model

    User = get_user_model()
    project = Project.objects.first()
    if not project:
        print("No project found. Run bootstrap_platform first.")
        return

    user = User.objects.filter(is_superuser=True).first() or User.objects.first()
    if not user:
        print("No user found. Run bootstrap_platform first.")
        return

    print(f"Seeding demo audits for project '{project.name}' as {user.username}")
    print(f"Target: {TARGET_MODEL_ID} | Auditor/Judge: {AUDITOR_MODEL_ID}")
    print(f"Scenarios per pack: {SCENARIOS_PER_PACK}")
    print()

    endpoints = ensure_simulachat_connection(project, user)

    # Check if demo runs already exist
    from audits.models import AuditRun
    existing = AuditRun.objects.filter(
        project=project,
        runtime_metadata__demo_seed=True,
    ).count()
    if existing > 0:
        print(f"\n{existing} demo audit(s) already exist. Skipping (delete them first to re-seed).")
        return

    packs_to_try = ["safety", "rag", "health"]
    created = 0
    for pack in packs_to_try:
        print(f"\n--- Running demo audit: {pack} ---")
        try:
            ok = run_demo_audit(project, user, pack, endpoints)
            if ok:
                created += 1
        except Exception as exc:
            logger.exception("Demo audit for '%s' failed: %s", pack, exc)
            print(f"  FAILED: {exc}")

    print(f"\nDone. Created {created} demo audit run(s).")
    print("Check the dashboard at /dashboard/")


if __name__ == "__main__":
    main()
