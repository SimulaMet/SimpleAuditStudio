"""Seed demo audit runs from pre-recorded results (no API key needed).

Loads real audit execution results captured in ``infra/fixtures/demo_audit_results.json``
and creates AuditRun + ScenarioResult rows so the dashboard is populated with
realistic data on first boot.

The fixture references model names that match the defaults created by
``seed_platform`` (GPT-4o, GPT-4o Mini). The seed looks up existing
ModelEndpoint rows by display_name — it does NOT create new models.

Usage:
    python manage.py seed_demo_audits [--project 1] [--force]

Idempotent: skips if demo runs already exist. Use --force to re-seed.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

logger = logging.getLogger("simpleaudit.seed_demo")

FIXTURE_PATH = Path(__file__).resolve().parent.parent.parent / "fixtures" / "demo_audit_results.json"


class Command(BaseCommand):
    help = "Seed demo audit runs from pre-recorded fixture (no API key needed)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--project", type=int, default=1,
            help="Project ID to seed (default: 1)",
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

        # Load fixture
        if not FIXTURE_PATH.exists():
            raise CommandError(
                f"Fixture not found: {FIXTURE_PATH}. "
                "Generate it by running audits and saving results."
            )
        with open(FIXTURE_PATH) as f:
            fixture_data = json.load(f)

        meta = fixture_data.pop("_meta", {})
        target_name = meta.get("target_model", "GPT-4o")
        auditor_name = meta.get("auditor_model", "GPT-4o Mini")
        judge_name = meta.get("judge_model", "GPT-4o Mini")

        # Look up EXISTING model endpoints by display_name (created by seed_platform)
        from model_registry.models import ModelEndpoint
        target_ep = ModelEndpoint.objects.filter(project=project, display_name=target_name).first()
        auditor_ep = ModelEndpoint.objects.filter(project=project, display_name=auditor_name).first()
        judge_ep = ModelEndpoint.objects.filter(project=project, display_name=judge_name).first()

        missing = [n for n, ep in [(target_name, target_ep), (auditor_name, auditor_ep), (judge_name, judge_ep)] if not ep]
        if missing:
            raise CommandError(
                f"Model endpoint(s) not found: {', '.join(missing)}. "
                "Run 'manage.py seed_platform' first to create default models."
            )

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
            f"  Target:  {target_name}\n"
            f"  Auditor: {auditor_name}\n"
            f"  Judge:   {judge_name}\n"
            f"  Packs: {', '.join(fixture_data.keys())}\n"
            f"  Source: pre-recorded fixture (no API calls)"
        )

        created = 0
        for pack_name, scenarios in fixture_data.items():
            ok = self._create_run_from_fixture(project, user, pack_name, scenarios, target_ep, auditor_ep, judge_ep)
            if ok:
                created += 1

        self.stdout.write(self.style.SUCCESS(f"\nDone. Created {created} demo audit run(s)."))

    def _create_run_from_fixture(self, project, user, pack_name: str, scenarios: list[dict],
                                  target_ep, auditor_ep, judge_ep) -> bool:
        """Create an AuditRun + ScenarioResults from fixture data."""
        from audits.events import append_event, upsert_scenario_result
        from audits.models import AuditRun
        from infra.simpleaudit_package import resolve_engine_provenance
        from scenarios.models import ScenarioSet

        set_obj = ScenarioSet.objects.filter(project=project, name=f"SimpleAudit: {pack_name}").first()
        if not set_obj:
            self.stderr.write(f"  No scenario set '{pack_name}' found — skipping.")
            return False
        version = set_obj.versions.order_by("-version").first()
        if not version:
            self.stderr.write(f"  No published version for '{pack_name}' — skipping.")
            return False

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
                "api_key_direct": "",
                "enabled": ep.enabled,
            }

        provenance = resolve_engine_provenance()
        now = timezone.now()
        n_scenarios = len(scenarios)

        run = AuditRun.objects.create(
            project=project,
            name=f"Demo: {pack_name} safety check",
            status=AuditRun.Status.COMPLETED,
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
            runtime_metadata={
                "created_by_username": user.username,
                "demo_seed": True,
                "source": "pre_recorded_fixture",
            },
            queued_at=now - timedelta(hours=2, minutes=30),
            started_at=now - timedelta(hours=2, minutes=28),
            finished_at=now - timedelta(hours=2),
            total_scenarios=n_scenarios,
            created_by=user,
        )

        items = list(version.items.select_related("scenario", "revision").order_by("position"))
        severities = []
        successful = 0
        failed = 0

        for i, sc_data in enumerate(scenarios):
            if i >= len(items):
                break
            item = items[i]
            vid = str(item.id)
            result = sc_data["result"]
            severity = result.get("severity", "")
            is_error = severity.upper() == "ERROR"
            status = "failed" if is_error else "completed"

            upsert_scenario_result(run.id, vid, status=status, attempts=1, result=result)

            if is_error:
                failed += 1
            else:
                successful += 1
                if severity:
                    severities.append(severity)

            append_event(run.id, vid, "scenario_attempted", {"attempt": 1})
            append_event(
                run.id, vid,
                "scenario_completed" if not is_error else "scenario_failed",
                {"attempt": 1, "severity": severity},
            )

        run.completed_scenarios = successful + failed
        run.successful_scenarios = successful
        run.failed_scenarios = failed
        run.summary_metrics = {
            "total": successful + failed,
            "passed": successful,
            "failed": failed,
            "pass_rate": round(successful / max(successful + failed, 1), 3),
            "severity_distribution": _count_severities(severities),
        }
        run.save(update_fields=[
            "status", "finished_at", "completed_scenarios",
            "successful_scenarios", "failed_scenarios", "summary_metrics",
        ])

        append_event(run.id, "_run", "run_queued", {"scenarios": n_scenarios})
        append_event(run.id, "_run", "run_stage", {"stage": "aggregation"})
        append_event(run.id, "_run", "run_completed", {"scenarios": successful + failed})

        self.stdout.write(f"  ✓ {pack_name}: {successful}/{successful + failed} passed")
        return True


def _count_severities(severities: list[str]) -> dict:
    counts: dict[str, int] = {}
    for s in severities:
        key = s.upper() if s else "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    return counts
