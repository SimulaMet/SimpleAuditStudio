"""Seed demo audit runs from pre-recorded results (no API key needed).

Loads real audit execution results captured in ``infra/fixtures/demo_audit_results.json``
and creates AuditRun + ScenarioResult rows so the dashboard is populated with
realistic data on first boot.

The fixture references model names that match the defaults created by
``seed_platform`` (GPT-4o, GPT-4o Mini). The seed looks up existing
RegisteredModel rows by display_name — it does NOT create new models.

Fixture structure:
{
  "_meta": {"target_model": "GPT-4o", "auditor_model": "GPT-4o Mini", ...},
  "runs": [
    {"pack": "safety", "label": "safety baseline", "experiment": "...", "scenarios": [...]},
    {"pack": "safety", "label": "...", "experiment": "...", "generation": {"target_params": {...}}, ...},
    ...
  ]
}

Multiple runs can share the same "pack" (scenario set), enabling meaningful
comparison via intersection. Runs with the same "experiment" are grouped into
one Experiment, whose factors are the inputs that differ between its runs;
"generation" is added to a run's generation settings. A run may name its own
"judge" (a SimpleAudit judge key, e.g. "decision_safety") and "judge_model"
(a display name, e.g. "Jev (TypeSafe)"); a run whose judge model is missing is
skipped.

Usage:
    python manage.py seed_demo_audits [--project 1] [--force]

Idempotent: runs that already exist (by name) are skipped, so a fixture run
added later still reaches workspaces seeded before it. Use --force to re-seed.
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
        from django.contrib.auth import get_user_model

        from accounts.models import Project

        User = get_user_model()
        project_id = options["project"]
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            raise CommandError(f"Project {project_id} not found. Run bootstrap_platform first.")

        user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        if not user:
            raise CommandError("No user found. Run bootstrap_platform first.")

        if not FIXTURE_PATH.exists():
            raise CommandError(f"Fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH) as f:
            fixture_data = json.load(f)

        meta = fixture_data.get("_meta", {})
        runs_spec = fixture_data.get("runs", [])
        if not runs_spec:
            raise CommandError("Fixture has no 'runs' entries.")

        target_name = meta.get("target_model", "GPT-4o")
        auditor_name = meta.get("auditor_model", "GPT-4o Mini")
        judge_name = meta.get("judge_model", "GPT-4o Mini")

        from model_registry.models import RegisteredModel
        target_ep = RegisteredModel.objects.filter(project=project, display_name=target_name).first()
        auditor_ep = RegisteredModel.objects.filter(project=project, display_name=auditor_name).first()
        judge_ep = RegisteredModel.objects.filter(project=project, display_name=judge_name).first()

        missing = [n for n, ep in [(target_name, target_ep), (auditor_name, auditor_ep), (judge_name, judge_ep)] if not ep]
        if missing:
            raise CommandError(
                f"Model endpoint(s) not found: {', '.join(missing)}. "
                "Run 'manage.py seed_platform' first to create default models."
            )

        from audits.models import AuditRun, Experiment
        existing = AuditRun.objects.filter(project=project, runtime_metadata__demo_seed=True)
        if existing.exists():
            if options["force"]:
                self.stdout.write(f"Deleting {existing.count()} existing demo run(s)...")
                experiment_ids = set(
                    Experiment.runs.through.objects.filter(run__in=existing).values_list("experiment_id", flat=True)
                )
                existing.delete()
                # Demo experiments left without runs go too (never a user's own).
                Experiment.objects.filter(pk__in=experiment_ids, runs__isnull=True).delete()
            else:
                # A run the fixture gained after this workspace was seeded
                # (e.g. the decision-model demo) is added once. Runs it was
                # already given stay as they are, deleted ones included.
                existing = list(existing)   # before adding: a lazy queryset would include them
                given = self._given_labels(project, runs_spec, existing)
                added = [
                    run for spec in runs_spec
                    if _label(spec) not in given
                    and (run := self._create_spec_run(project, user, spec, target_ep, auditor_ep, judge_ep))
                ]
                self._record_given(project, given | {_label(spec) for spec in runs_spec})
                # Seeded before demo experiments existed: group those runs now.
                made = self._group_into_experiments(project, user, runs_spec, existing + added)
                note = f" Added {len(added)} new demo run(s)." if added else ""
                note += f" Grouped runs into {made} experiment(s)." if made else ""
                self.stdout.write(f"{len(existing)} demo audit(s) already exist. Use --force to re-seed.{note}")
                return

        self.stdout.write(
            f"Seeding {len(runs_spec)} demo audit run(s) for '{project.name}'\n"
            f"  Target: {target_name} | Auditor/Judge: {auditor_name}\n"
            f"  Source: pre-recorded fixture (no API calls)"
        )

        runs = []
        for run_spec in runs_spec:
            run = self._create_spec_run(project, user, run_spec, target_ep, auditor_ep, judge_ep)
            if run is not None:
                runs.append(run)
        made = self._group_into_experiments(project, user, runs_spec, runs)
        self._record_given(project, {_label(spec) for spec in runs_spec})

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Created {len(runs)} demo audit run(s) in {made} experiment(s)."))

    @staticmethod
    def _given_labels(project, runs_spec: list[dict], existing) -> set[str]:
        """Labels of the fixture runs this workspace was already given.

        Recorded per project since the per-run judge was added. A workspace
        seeded before that has no record: every run it has, plus every run of
        the fixture as it was then (none named its own judge), counts as given.
        """
        from accounts.services import get_site_setting

        recorded = get_site_setting(_given_key(project))
        if isinstance(recorded, list):
            return set(recorded)
        have = {run.name.removeprefix("Demo: ") for run in existing}
        return have | {_label(spec) for spec in runs_spec if not spec.get("judge")}

    @staticmethod
    def _record_given(project, labels: set[str]) -> None:
        from accounts.services import set_site_setting

        set_site_setting(_given_key(project), sorted(labels))

    @staticmethod
    def _group_into_experiments(project, user, runs_spec: list[dict], runs: list) -> int:
        """Put demo runs into the experiments the fixture names ("experiment").
        Runs already in an experiment are left alone. Returns how many were made."""
        from audits.models import Experiment, ExperimentRun

        by_label = {f"Demo: {spec.get('label', spec['pack'])}": spec.get("experiment") for spec in runs_spec}
        # Experiments flagged "public" in the fixture are visible without login.
        public_names = {spec["experiment"] for spec in runs_spec if spec.get("experiment") and spec.get("public")}
        groups: dict[str, list] = {}
        for run in runs:
            name = by_label.get(run.name)
            if name and not run.experiments.exists():
                groups.setdefault(name, []).append(run)
        for name, members in groups.items():
            visibility = (
                Experiment.Visibility.PUBLIC if name in public_names else Experiment.Visibility.PRIVATE
            )
            experiment = Experiment.objects.create(
                project=project, name=name, created_by=user, visibility=visibility
            )
            ExperimentRun.objects.bulk_create(
                [ExperimentRun(experiment=experiment, run=run, added_by=user) for run in members]
            )
        return len(groups)

    @staticmethod
    def _judge(project, user, key: str | None = None):
        """The judge version that graded a fixture run: SimpleAudit's default
        judge, or the workspace's judge based on ``key`` (e.g. "decision_safety")."""
        from judges.models import JudgeVersion
        from judges.services import default_judge_version, ensure_starter_judges

        if not key:
            return default_judge_version(project, user)
        ensure_starter_judges(project, user)
        version = (
            JudgeVersion.objects.filter(judge__project=project, base=key, criteria="")
            .order_by("judge__created_at", "-version").first()
        )
        return version

    def _create_spec_run(self, project, user, run_spec: dict, target_ep, auditor_ep, judge_ep):
        """One fixture run, with its own judge and judge model when it names them."""
        from model_registry.models import RegisteredModel

        label = run_spec.get("label", run_spec["pack"])
        judge_name = run_spec.get("judge_model")
        if judge_name:
            judge_ep = RegisteredModel.objects.filter(project=project, display_name=judge_name).first()
            if judge_ep is None:
                self.stderr.write(f"  No model '{judge_name}' for '{label}' — skipping.")
                return None
        judge_version = self._judge(project, user, run_spec.get("judge"))
        if judge_version is None:
            self.stderr.write(f"  No judge '{run_spec.get('judge')}' for '{label}' — skipping.")
            return None
        return self._create_run(project, user, run_spec["pack"], label, run_spec["scenarios"],
                                target_ep, auditor_ep, judge_ep, generation=run_spec.get("generation"),
                                judge_version=judge_version)

    def _create_run(self, project, user, pack: str, label: str, scenarios: list[dict],
                    target_ep, auditor_ep, judge_ep, generation: dict | None = None,
                    judge_version=None):
        """Create one completed demo run from recorded results; None when its set is missing."""
        from audits.events import append_event, upsert_scenario_result
        from audits.models import AuditRun
        from infra.simpleaudit_package import resolve_engine_provenance
        from scenarios.models import ScenarioSet

        set_obj = ScenarioSet.objects.filter(project=project, name=f"SimpleAudit: {pack}").first()
        if not set_obj:
            self.stderr.write(f"  No scenario set '{pack}' — skipping.")
            return None
        version = set_obj.versions.order_by("-version").first()
        if not version:
            self.stderr.write(f"  No published version for '{pack}' — skipping.")
            return None

        from audits.services import _endpoint_snapshot as _snap
        from judges.services import judge_snapshot

        judge_version = judge_version or self._judge(project, user)

        provenance = resolve_engine_provenance()
        now = timezone.now()
        n = len(scenarios)

        # The fixture's "generation" (e.g. a hotter target) on top of the defaults.
        gen_params = {"max_turns": 3, "language": "English", **(generation or {})}

        run = AuditRun.objects.create(
            project=project,
            name=f"Demo: {label}",
            status=AuditRun.Status.COMPLETED,
            scenario_set_version=version,
            target_model=target_ep,
            auditor_model=auditor_ep,
            judge_version=judge_version,
            judge_model=judge_ep,
            target_config_snapshot=_snap(target_ep),
            auditor_config_snapshot=_snap(auditor_ep),
            judge_config_snapshot={**_snap(judge_ep), "judge": judge_snapshot(judge_version)},
            generation_parameters_snapshot=gen_params,
            simpleaudit_version=provenance.version or "unknown",
            git_commit=provenance.commit or "",
            runtime_metadata={"created_by_username": user.username, "demo_seed": True, "source": "pre_recorded_fixture"},
            queued_at=now - timedelta(hours=3, minutes=30),
            started_at=now - timedelta(hours=3, minutes=28),
            finished_at=now - timedelta(hours=3),
            total_scenarios=n,
            created_by=user,
        )

        items = list(version.items.select_related("scenario", "revision").order_by("position"))
        severities = []
        successful = failed = 0

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
            append_event(run.id, vid, "scenario_completed" if not is_error else "scenario_failed",
                         {"attempt": 1, "severity": severity})

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
        run.save(update_fields=["status", "finished_at", "completed_scenarios",
                                "successful_scenarios", "failed_scenarios", "summary_metrics"])

        append_event(run.id, "_run", "run_queued", {"scenarios": n})
        append_event(run.id, "_run", "run_stage", {"stage": "aggregation"})
        append_event(run.id, "_run", "run_completed", {"scenarios": successful + failed})

        self.stdout.write(f"  ✓ #{run.id} {label}: {successful}/{successful + failed} passed")
        return run


def _label(spec: dict) -> str:
    return spec.get("label", spec["pack"])


def _given_key(project) -> str:
    return f"demo_audits_given:{project.id}"


def _count_severities(severities: list[str]) -> dict:
    counts: dict[str, int] = {}
    for s in severities:
        key = s.upper() if s else "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    return counts
