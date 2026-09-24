"""Seed realistic demo audit runs using a configured model endpoint.

Runs small audits (a subset of scenarios from existing packs) to populate
the dashboard with real execution data. Uses the SimulaChat endpoint by
default; override via environment variables.

Usage:
    python manage.py seed_demo_audits [--packs safety rag health] [--scenarios-per-pack 3]

Environment:
    SIMULACHAT_BASE_URL   (default: https://simulachat.sushant.pp.ua/api/v1)
    SIMULACHAT_API_KEY    (required for actual execution)
    DEMO_TARGET_MODEL     (default: Qwen3.8-27B)
    DEMO_AUDITOR_MODEL    (default: glm-5-2-fp8)
    DEMO_JUDGE_MODEL      (default: glm-5-2-fp8)

Idempotent: skips if demo runs already exist for the project.
Set SEED_DEMO_AUDITS=false in the environment to disable on boot.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = logging.getLogger("simpleaudit.seed_demo")


class Command(BaseCommand):
    help = "Seed demo audit runs with real model execution (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--project", type=int, default=1,
            help="Project ID to seed (default: 1)",
        )
        parser.add_argument(
            "--packs", nargs="+", default=["safety", "rag", "health"],
            help="Scenario packs to run (default: safety rag health)",
        )
        parser.add_argument(
            "--scenarios-per-pack", type=int, default=3,
            help="Number of scenarios per pack (default: 3)",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-seed even if demo runs already exist (deletes old ones first)",
        )

    def handle(self, *args, **options):
        from accounts.models import Project
        from django.contrib.auth import get_user_model

        User = get_user_model()
        project_id = options["project"]
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            raise CommandError(f"Project {project_id} not found. Run bootstrap_platform first.")

        user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        if not user:
            raise CommandError("No user found. Run bootstrap_platform first.")

        api_key = os.environ.get("SIMULACHAT_API_KEY", "").strip()
        if not api_key:
            self.stderr.write(
                self.style.WARNING(
                    "SIMULACHAT_API_KEY not set — cannot execute demo audits. "
                    "Set the env var and re-run."
                )
            )
            return

        base_url = os.environ.get("SIMULACHAT_BASE_URL", "https://simulachat.sushant.pp.ua/api/v1")
        target_model = os.environ.get("DEMO_TARGET_MODEL", "Qwen3.8-27B")
        auditor_model = os.environ.get("DEMO_AUDITOR_MODEL", "glm-5-2-fp8")
        judge_model = os.environ.get("DEMO_JUDGE_MODEL", "glm-5-2-fp8")
        n_scenarios = options["scenarios_per_pack"]

        # Idempotency check
        from audits.models import AuditRun
        existing = AuditRun.objects.filter(project=project, runtime_metadata__demo_seed=True)
        if existing.exists():
            if options["force"]:
                self.stdout.write(f"Deleting {existing.count()} existing demo run(s)...")
                existing.delete()
            else:
                self.stdout.write(
                    f"{existing.count()} demo audit(s) already exist. "
                    "Use --force to re-seed."
                )
                return

        self.stdout.write(
            f"Seeding demo audits for '{project.name}' as {user.username}\n"
            f"  Target:  {target_model}\n"
            f"  Auditor: {auditor_model}\n"
            f"  Judge:   {judge_model}\n"
            f"  Scenarios/pack: {n_scenarios}\n"
            f"  Packs: {', '.join(options['packs'])}"
        )

        endpoints = self._ensure_connection(project, user, base_url, api_key, target_model, auditor_model)

        created = 0
        for pack in options["packs"]:
            self.stdout.write(f"\n--- Running demo audit: {pack} ---")
            try:
                ok = self._run_demo_audit(project, user, pack, endpoints, n_scenarios)
                if ok:
                    created += 1
            except Exception as exc:
                logger.exception("Demo audit for '%s' failed: %s", pack, exc)
                self.stderr.write(self.style.ERROR(f"  FAILED: {exc}"))

        self.stdout.write(self.style.SUCCESS(f"\nDone. Created {created} demo audit run(s)."))

    def _ensure_connection(self, project, user, base_url, api_key, target_model, auditor_model):
        """Create ModelEndpoints for the demo models if missing."""
        from model_registry.models import ModelConnection, ModelEndpoint, RegisteredModel

        conn, _ = ModelConnection.objects.get_or_create(
            project=project,
            name="SimulaChat",
            defaults={
                "provider": "openai",
                "base_url": base_url,
                "api_key_direct": api_key,
                "enabled": True,
                "created_by": user,
            },
        )

        endpoints = {}
        for display_name, model_id in [
            (target_model, target_model),
            (auditor_model, auditor_model),
        ]:
            ep, created = ModelEndpoint.objects.get_or_create(
                project=project,
                display_name=display_name,
                defaults={
                    "provider": "openai",
                    "base_url": base_url,
                    "model_id": model_id,
                    "enabled": True,
                    "default_parameters": {"temperature": 0.7, "max_tokens": 4096},
                    "api_key_direct": api_key,
                    "created_by": user,
                },
            )
            endpoints[model_id] = ep
            if created:
                self.stdout.write(f"  + {display_name} ({model_id})")

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

    def _get_version(self, project, pack_name: str):
        from scenarios.models import ScenarioSet

        set_obj = ScenarioSet.objects.filter(project=project, name=f"SimpleAudit: {pack_name}").first()
        if not set_obj:
            return None
        version = set_obj.versions.order_by("-version").first()
        if not version or not version.items.exists():
            return None
        return version

    def _run_demo_audit(self, project, user, pack_name: str, endpoints: dict, n_scenarios: int) -> bool:
        from audits.events import append_event, upsert_scenario_result, get_result
        from audits.models import AuditRun
        from infra.engine import EngineError, run_scenario
        from infra.simpleaudit_package import resolve_engine_provenance

        version = self._get_version(project, pack_name)
        if not version:
            self.stderr.write(f"  No scenario set version for '{pack_name}' — skipping.")
            return False

        items = list(version.items.select_related("scenario", "revision").order_by("position"))[:n_scenarios]
        if not items:
            self.stderr.write(f"  Pack '{pack_name}' has no scenarios — skipping.")
            return False

        provenance = resolve_engine_provenance()
        target_ep = endpoints[list(endpoints.keys())[0]]
        auditor_ep = endpoints[list(endpoints.keys())[-1]]
        judge_ep = auditor_ep  # same model for auditor and judge

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

        append_event(run.id, "_run", "run_queued", {"scenarios": len(items)})
        append_event(run.id, "_run", "run_stage", {"stage": "preparing"})

        completed = successful = failed = 0
        severities = []

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
                if severity:
                    severities.append(severity)

            append_event(
                run.id, vid,
                "scenario_completed" if not is_error else "scenario_failed",
                {"attempt": 1, "severity": severity},
            )
            self.stdout.write(f"  [{completed}/{len(items)}] {scenario.title} → {status} ({severity})")

        # Finalize
        run.completed_scenarios = completed
        run.successful_scenarios = successful
        run.failed_scenarios = failed
        run.status = AuditRun.Status.COMPLETED
        run.finished_at = timezone.now()
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
        return True


def _count_severities(severities: list[str]) -> dict:
    counts: dict[str, int] = {}
    for s in severities:
        key = s.upper() if s else "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    return counts
