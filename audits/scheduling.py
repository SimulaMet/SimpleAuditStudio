"""Recurring audits (AuditSchedule) and drift statistics over their runs.

The tick is DB-driven: ``run_due_schedules`` is called every sweeper pass by the
worker (and by ``manage.py run_schedules`` for an external cron). It claims due
schedules with ``select_for_update(skip_locked=True)`` so concurrent workers
never launch the same tick twice, creates an ordinary frozen AuditRun per due
schedule, and submits it outside the claim transaction (same rule as the UI:
a downed job system must not roll back a frozen experiment record).
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, timedelta

from django.db import transaction
from django.utils import timezone

from audits.events import ScenarioResult
from audits.models import AuditRun, AuditSchedule

logger = logging.getLogger("simpleaudit.schedule")

_TERMINAL = {AuditRun.Status.COMPLETED, AuditRun.Status.FAILED, AuditRun.Status.CANCELLED}
_FORM_KEYS = ("max_turns", "language", "n_repetitions")

# Schedules spend the workspace's API keys unattended, so they are capped.
MAX_SCHEDULES_PER_PROJECT = 10
# Bounds for fixed-interval schedules. Cron expressions have no minimum gap:
# a tick is skipped while the previous run is still active, so runs of one
# schedule never overlap however often the expression fires.
MIN_INTERVAL_HOURS = 6
MAX_INTERVAL_HOURS = 24 * 90

# Previous completed runs pooled as the baseline a new point is tested against.
BASELINE_WINDOW = 4
# Two-sided 95% critical value for the two-proportion z-test.
Z_CRIT = 1.96


# ─── Permissions ─────────────────────────────────────────────────────────────
# View: any member. Create: admin or auditor. Run now / pause / delete: admin,
# or the auditor who created the schedule. Each tick re-checks that the creator
# still holds admin/auditor; if not, the schedule pauses itself.

def _write_roles():
    from accounts.models import ProjectMembership

    return (ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)


def _role(user, project) -> str | None:
    from accounts.models import ProjectMembership

    if not user or not user.is_authenticated:
        return None
    return (
        ProjectMembership.objects.filter(project=project, user=user)
        .values_list("role", flat=True)
        .first()
    )


def has_write_role(user, project) -> bool:
    """Admin or auditor (or superuser): may launch audits and create schedules."""
    if user and user.is_authenticated and user.is_superuser:
        return True
    return _role(user, project) in _write_roles()


def can_manage_schedule(user, schedule: AuditSchedule) -> bool:
    from accounts.models import ProjectMembership

    if user and user.is_authenticated and user.is_superuser:
        return True
    role = _role(user, schedule.project)
    if role == ProjectMembership.Role.ADMIN:
        return True
    return role == ProjectMembership.Role.AUDITOR and schedule.created_by_id == user.id


def owner_authorized(schedule: AuditSchedule) -> bool:
    """Whether the creator may still launch runs for this schedule."""
    owner = schedule.created_by
    if owner is None or not owner.is_active:
        return False
    return has_write_role(owner, schedule.project)


def cron_next(expr: str, after):
    """First firing of ``expr`` (UTC) strictly after ``after``."""
    from cronsim import CronSim

    return next(CronSim(expr, after.astimezone(UTC)))


def validate_cron(expr: str, now=None) -> str:
    """Normalise and validate a cron expression; raise ValueError if unusable.

    Rejects syntax errors and expressions that never fire. There is no minimum
    frequency (see ``MIN_INTERVAL_HOURS``).
    """
    from cronsim import CronSim, CronSimError

    expr = " ".join((expr or "").split())
    if len(expr.split(" ")) != 5:
        raise ValueError("Cron expression needs 5 fields: minute hour day-of-month month day-of-week.")
    now = now or timezone.now()
    try:
        next(CronSim(expr, now.astimezone(UTC)))
    except (CronSimError, StopIteration) as exc:
        raise ValueError(f"Invalid cron expression: {exc or 'never fires'}.") from exc
    return expr


def next_after(schedule: AuditSchedule, now):
    """Next tick strictly after ``now`` for either timing rule."""
    if schedule.cron_expression:
        return cron_next(schedule.cron_expression, now)
    return advance(schedule.next_run_at, schedule.interval_hours, now)


def advance(next_run_at, interval_hours: int, now):
    """Next tick strictly after ``now``, on the schedule's grid.

    Missed ticks (worker down for a week) are skipped rather than replayed, so a
    recovering worker launches one run, not a burst of stale ones.
    """
    step = timedelta(hours=max(interval_hours, 1))
    if next_run_at > now:
        return next_run_at
    missed = int((now - next_run_at) / step) + 1
    return next_run_at + missed * step


def resolve_version(schedule: AuditSchedule):
    if schedule.scenario_set_version_id:
        return schedule.scenario_set_version
    return schedule.scenario_set.versions.order_by("-version").first()


def launch_schedule(schedule: AuditSchedule, *, now=None) -> AuditRun:
    """Create (not submit) one frozen AuditRun for ``schedule``."""
    from audits.services import create_audit_run

    now = now or timezone.now()
    version = resolve_version(schedule)
    if version is None:
        raise ValueError("Scenario set has no published version.")
    if schedule.created_by is None:
        raise ValueError("Schedule owner no longer exists; recreate the schedule.")
    params = dict(schedule.generation_parameters or {})
    overrides = {k: params.pop(k, None) for k in _FORM_KEYS}
    return create_audit_run(
        project=schedule.project,
        user=schedule.created_by,
        name=f"{schedule.name} · {now:%Y-%m-%d %H:%M}",
        scenario_set_version=version,
        target_model=schedule.target_model,
        auditor_model=schedule.auditor_model,
        judge_model=schedule.judge_model,
        max_turns_override=overrides["max_turns"],
        language_override=overrides["language"],
        n_repetitions_override=overrides["n_repetitions"],
        gen_config_override=params or None,
        schedule=schedule,
    )


def run_due_schedules(now=None) -> list[int]:
    """Launch every enabled schedule whose ``next_run_at`` has passed.

    Returns the ids of the runs created. A tick whose previous run is still in
    flight is skipped (never overlap runs of one series); a tick that fails to
    create its run records ``last_error`` and still advances, so one broken
    schedule cannot wedge the loop. A schedule whose creator lost the
    admin/auditor role is paused instead of launched.
    """
    from audits.services import submit_audit_run

    now = now or timezone.now()
    created: list[AuditRun] = []
    with transaction.atomic():
        due = (
            AuditSchedule.objects.select_for_update(skip_locked=True)
            .filter(enabled=True, next_run_at__lte=now)
            .select_related("last_run", "project", "created_by")
            .order_by("next_run_at")
        )
        for schedule in due:
            schedule.next_run_at = next_after(schedule, now)
            schedule.last_tick_at = now
            last = schedule.last_run
            if not owner_authorized(schedule):
                schedule.enabled = False
                who = schedule.created_by.username if schedule.created_by else "deleted user"
                schedule.last_error = (
                    f"Paused {now:%Y-%m-%d %H:%M}: owner {who} no longer has admin or auditor role "
                    "in this workspace. Recreate the schedule under an authorized user."
                )
                logger.warning("Schedule %s paused: owner lost write role", schedule.pk)
            elif last is not None and last.status not in _TERMINAL:
                schedule.last_error = f"Skipped {now:%Y-%m-%d %H:%M}: run #{last.id} still {last.status}."
            else:
                try:
                    run = launch_schedule(schedule, now=now)
                except Exception as exc:  # noqa: BLE001 - recorded on the schedule, loop continues
                    schedule.last_error = f"{now:%Y-%m-%d %H:%M}: {exc}"
                    logger.warning("Schedule %s tick failed: %s", schedule.pk, exc)
                else:
                    schedule.last_run = run
                    schedule.last_error = ""
                    created.append(run)
            schedule.save(
                update_fields=["enabled", "next_run_at", "last_tick_at", "last_run", "last_error", "updated_at"]
            )

    for run in created:
        try:
            submit_audit_run(run)
        except Exception as exc:  # noqa: BLE001 - run stays queued; the sweeper retries submission
            logger.warning("Scheduled run %s not submitted: %s", run.pk, exc)
    if created:
        logger.info("Schedules launched %d run(s): %s", len(created), [r.pk for r in created])
    return [r.pk for r in created]


# ─── Drift statistics ────────────────────────────────────────────────────────

def _trial_severities(result: dict) -> list[str]:
    """Every judged trial in one ScenarioResult (one per repetition)."""
    reps = result.get("reps")
    if isinstance(reps, list):
        return [(r or {}).get("severity") for r in reps]
    return [result.get("severity")]


def pass_counts(run_ids: list[int]) -> dict[int, dict]:
    """Per run: judged trials ``n``, passing trials ``k`` and errored trials.

    Repetitions count as separate trials, so the confidence interval narrows as
    ``n_repetitions`` grows. ERROR severities and non-completed scenarios are
    excluded from ``n`` (an outage is not a behaviour change) and counted apart.
    """
    out = {rid: {"k": 0, "n": 0, "errors": 0} for rid in run_ids}
    rows = ScenarioResult.objects.filter(run_id__in=run_ids).only("run_id", "status", "result")
    for row in rows:
        c = out.get(row.run_id)
        if c is None:
            continue
        if row.status != "completed":
            c["errors"] += 1
            continue
        for sev in _trial_severities(row.result or {}):
            if not sev or sev == "ERROR":
                c["errors"] += 1
                continue
            c["n"] += 1
            if sev == "pass":
                c["k"] += 1
    return out


def wilson(k: int, n: int, z: float = Z_CRIT) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> float | None:
    """z statistic for p2 - p1 (pooled); None when undefined."""
    if n1 == 0 or n2 == 0:
        return None
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    return (k2 / n2 - k1 / n1) / se


def drift_series(schedule: AuditSchedule) -> list[dict]:
    """Chronological points for the schedule's runs with CI and change flags.

    Each completed run is tested against the pooled previous ``BASELINE_WINDOW``
    completed runs on the same scenario set version; ``change`` is
    "drop"/"rise" when |z| >= 1.96, else "".
    """
    runs = list(
        schedule.runs.filter(archived=False)
        .select_related("scenario_set_version")
        .order_by("created_at")
    )
    counts = pass_counts([r.id for r in runs])
    points: list[dict] = []
    history: list[dict] = []
    for run in runs:
        c = counts[run.id]
        point = {
            "run": run,
            "k": c["k"],
            "n": c["n"],
            "errors": c["errors"],
            "rate": (c["k"] / c["n"]) if c["n"] else None,
            "lo": None,
            "hi": None,
            "z": None,
            "change": "",
            "version": run.scenario_set_version.version,
            "version_changed": bool(points) and points[-1]["version"] != run.scenario_set_version.version,
        }
        if c["n"]:
            point["lo"], point["hi"] = wilson(c["k"], c["n"])
        if point["version_changed"]:
            # Different scenarios: the old baseline is not comparable.
            history = []
        if run.status == AuditRun.Status.COMPLETED and c["n"]:
            window = history[-BASELINE_WINDOW:]
            bk = sum(p["k"] for p in window)
            bn = sum(p["n"] for p in window)
            z = two_proportion_z(bk, bn, c["k"], c["n"])
            if z is not None:
                point["z"] = z
                if z <= -Z_CRIT:
                    point["change"] = "drop"
                elif z >= Z_CRIT:
                    point["change"] = "rise"
            history.append(point)
        points.append(point)
    return points
