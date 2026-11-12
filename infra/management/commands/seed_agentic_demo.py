"""Load the recorded agentic demo run (no model calls, no API key).

``infra/fixtures/agentic_demo_run.json`` is a real, executed agentic audit of
the demo "Support Refund Assistant" agent against the default agentic pack
(D01-D10), captured with ``capture_agentic_demo_fixture --run N
--require-recorded --relabel-models "Demo model (recorded)"``: its results,
its agentic evaluation and the OTLP spans Open WebUI exported, with the
models it ran on relabelled neutrally (it was recorded on a model new
workspaces do not have).

The run is attached to the workspace's demo agent and scenario set. Its
frozen snapshots come from the fixture; the auditor/judge model rows point at
the seeded OpenAI models, so cloning the run uses a connection a new user can
configure. It is grouped into a public experiment, like the other demo runs,
so a public deployment shows it without sign-in. The seed records that a
workspace was given the run, so deleting it does not bring it back on the
next boot.

    python manage.py seed_agentic_demo [--project 1] [--force]
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

FIXTURE_PATH = Path(__file__).resolve().parent.parent.parent / "fixtures" / "agentic_demo_run.json"
SCENARIO_SET_NAME = "Agentic Audit: Explanatory Scenarios"
AGENT_NAME = "Support Refund Assistant"
#: Seeded model rows the run points at (snapshots carry the recorded labels).
FALLBACK_MODEL = "GPT-4o Mini"
#: The public experiment the run is shown in, like the other demo runs.
EXPERIMENT_NAME = "Agentic: auditing the Support Refund Assistant (recorded)"


def _given_key(project) -> str:
    return f"agentic_demo_given:{project.id}"


class Command(BaseCommand):
    help = "Load the recorded agentic demo run from its fixture (no model calls)."

    def add_arguments(self, parser):
        parser.add_argument("--project", type=int, default=1, help="Project id (default: 1).")
        parser.add_argument("--force", action="store_true", help="Replace an existing agentic demo run.")
        parser.add_argument("--fixture", default=str(FIXTURE_PATH), help="Fixture path.")

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model

        from accounts.models import Project
        from accounts.services import get_site_setting, set_site_setting
        from audits.agentic.demo_fixture import validate_fixture
        from audits.models import AuditRun

        project = Project.objects.filter(pk=options["project"]).first()
        user = get_user_model().objects.filter(is_superuser=True).order_by("id").first()
        if project is None or user is None:
            raise CommandError("Project and superuser are required; run bootstrap_platform first.")

        existing = AuditRun.objects.filter(project=project, runtime_metadata__agentic_demo_seed=True)
        if options["force"]:
            existing.delete()
        elif existing.exists():
            run = existing.first()
            # Loaded before demo runs were grouped: put it in its experiment now.
            note = " Grouped it into a public experiment." if group_into_experiment(run, user) else ""
            self.stdout.write(f"Agentic demo run already exists (run #{run.pk}).{note}")
            return
        elif get_site_setting(_given_key(project)):
            self.stdout.write("Agentic demo run was already given to this workspace (deleted since); skipped.")
            return

        path = Path(options["fixture"])
        try:
            fixture = json.loads(path.read_text())
            validate_fixture(fixture, require_recorded=True)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CommandError(f"Invalid recorded agentic fixture {path}: {exc}") from exc

        run = load_recorded_run(fixture, project=project, user=user)
        if run is None:
            self.stdout.write("Agentic demo run skipped: the demo agent or the agentic scenario set is missing.")
            return
        group_into_experiment(run, user)
        set_site_setting(_given_key(project), True)
        self.stdout.write(self.style.SUCCESS(
            f"Loaded recorded agentic demo run #{run.pk} ({run.total_scenarios} scenarios, no model calls)."
        ))


def group_into_experiment(run, user) -> bool:
    """Put the run in its public demo experiment unless it is in one already.

    Returns whether an experiment was created.
    """
    from audits.models import Experiment, ExperimentRun

    if run.experiments.exists():
        return False
    experiment = Experiment.objects.create(
        project=run.project, name=EXPERIMENT_NAME, created_by=user,
        visibility=Experiment.Visibility.PUBLIC,
    )
    ExperimentRun.objects.create(experiment=experiment, run=run, added_by=user)
    return True


def load_recorded_run(fixture: dict, *, project, user):
    """Create the completed run, its results and its spans from ``fixture``.

    Returns the run, or ``None`` when the workspace lacks the demo agent, the
    agentic scenario set, or a scenario the fixture recorded.
    """
    from audits.agentic.metrics import compute_run_metrics
    from audits.events import append_event, upsert_scenario_result
    from audits.models import AuditRun
    from judges.services import default_judge_version, judge_snapshot
    from model_registry.models import Agent, OtlpSpan, RegisteredModel
    from model_registry.services import agent_target_model
    from scenarios.models import ScenarioSet

    agent = Agent.objects.filter(project=project, name=AGENT_NAME).select_related("base_model").first()
    scenario_set = ScenarioSet.objects.filter(project=project, name=SCENARIO_SET_NAME).first()
    version = scenario_set.versions.order_by("-version").first() if scenario_set else None
    if agent is None or version is None:
        return None
    items = {item.scenario.key.lower(): item for item in version.items.select_related("scenario")}
    specs = fixture["scenario_results"]
    if any(str(spec["scenario_key"]).lower() not in items for spec in specs):
        return None

    # The agent's Open WebUI model row when it is synced; otherwise its base
    # model, so the run loads before chat is up (the snapshot names the agent).
    try:
        target_model = agent_target_model(agent) if agent.external_id else agent.base_model
    except ValueError:
        target_model = agent.base_model
    models = RegisteredModel.objects.filter(project=project, enabled=True)
    fallback = models.filter(display_name=FALLBACK_MODEL).first() or agent.base_model
    judge_version = default_judge_version(project, user)

    data = fixture["run"]
    recorded = fixture["recorded_run"]
    judge_config = {**(data.get("judge_config_snapshot") or {}), "judge": judge_snapshot(judge_version)}
    now = timezone.now()
    with transaction.atomic():
        run = AuditRun.objects.create(
            project=project,
            # Named like the other seeded runs, so it reads as a demo.
            name="Demo: " + (data.get("name") or "Agentic audit (recorded)").removeprefix("Demo: "),
            status=AuditRun.Status.COMPLETED,
            scenario_set_version=version,
            target_model=target_model,
            agent=agent,
            auditor_model=fallback,
            judge_version=judge_version,
            judge_model=fallback,
            target_config_snapshot=data.get("target_config_snapshot") or {},
            auditor_config_snapshot=data.get("auditor_config_snapshot") or {},
            judge_config_snapshot=judge_config,
            generation_parameters_snapshot=data.get("generation_parameters_snapshot") or {},
            agent_config_snapshot=data.get("agent_config_snapshot"),
            trace_config=data.get("trace_config") or {},
            simpleaudit_version=recorded.get("simpleaudit_version") or data.get("simpleaudit_version") or "unknown",
            git_commit=recorded.get("studio_git_commit") or data.get("git_commit") or "",
            runtime_metadata={
                "agentic_demo_seed": True,
                "source": "recorded_real_run_fixture",
                "fixture_version": fixture.get("fixture_version"),
                "fixture_checksum": (fixture.get("_meta") or {}).get("fixture_checksum", ""),
                "recorded_run": recorded,
            },
            queued_at=now,
            started_at=now,
            finished_at=now,
            total_scenarios=len(specs),
            completed_scenarios=len(specs),
            successful_scenarios=sum(str(spec.get("status", "")).lower() == "completed" for spec in specs),
            created_by=user,
        )
        for spec in specs:
            item = items[str(spec["scenario_key"]).lower()]
            status = spec.get("status") or "completed"
            upsert_scenario_result(run.pk, str(item.pk), status=status,
                                   attempts=spec.get("attempts", 1), result=spec.get("result") or {})
            append_event(run.pk, str(item.pk), "scenario_attempted", {"attempt": spec.get("attempts", 1)})
            append_event(run.pk, str(item.pk),
                         "scenario_completed" if status == "completed" else "scenario_failed",
                         {"attempt": spec.get("attempts", 1),
                          "severity": (spec.get("result") or {}).get("severity", "")})
        run.summary_metrics = {
            **(data.get("summary_metrics") or {}),
            **compute_run_metrics([spec.get("result") or {} for spec in specs]),
        }
        run.save(update_fields=["summary_metrics"])
        OtlpSpan.objects.bulk_create([
            OtlpSpan(
                target_id=span.get("target_id") or run.trace_config.get("target_id", ""),
                trace_id=span["trace_id"], span_id=span["span_id"],
                name=span.get("name") or "span", kind=span.get("kind") or "CHAIN",
                parent_span_id=span.get("parent_span_id"), start_time=span.get("start_time"),
                end_time=span.get("end_time"), status=span.get("status") or "OK",
                attributes=span.get("attributes") or {},
            )
            for span in fixture.get("spans") or []
        ], ignore_conflicts=True)
        append_event(run.pk, "_run", "run_queued", {"scenarios": len(specs)})
        append_event(run.pk, "_run", "run_completed",
                     {"scenarios": len(specs), "source": "recorded_real_run_fixture"})
    return run
