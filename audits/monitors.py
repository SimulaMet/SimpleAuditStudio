"""Recurring audits (Monitor) and drift statistics over their runs.

The tick is DB-driven: ``run_due_monitors`` is called every sweeper pass by the
worker (and by ``manage.py run_monitors`` for an external cron). It claims due
monitors with ``select_for_update(skip_locked=True)`` so concurrent workers
never launch the same tick twice, creates an ordinary frozen AuditRun per due
monitor, and submits it outside the claim transaction (same rule as the UI:
a downed job system must not roll back a frozen experiment record).
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, timedelta

from django.db import transaction
from django.utils import timezone

from audits.events import ScenarioResult
from audits.models import AuditRun, Monitor

logger = logging.getLogger("simpleaudit.monitor")

_FORM_KEYS = ("max_turns", "language", "n_repetitions")

# Monitors spend the workspace's API keys unattended, so they are capped.
MAX_MONITORS_PER_PROJECT = 50
# Bounds for fixed-interval monitors. Cron expressions have no minimum gap:
# a tick is skipped while the previous run is still active, so runs of one
# monitor never overlap however often the expression fires.
MIN_INTERVAL_HOURS = 6
MAX_INTERVAL_HOURS = 24 * 90

# Previous completed runs pooled as the baseline a new point is tested against.
BASELINE_WINDOW = 4
# Two-sided 95% critical value for the two-proportion z-test.
Z_CRIT = 1.96


# ─── Permissions ─────────────────────────────────────────────────────────────
# View: any member. Create: admin or auditor. Run now / pause / delete: admin,
# or the auditor who created the monitor. Each tick re-checks that the creator
# still holds admin/auditor; if not, the monitor pauses itself.

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
    """Admin or auditor (or superuser): may launch audits and create monitors."""
    if user and user.is_authenticated and user.is_superuser:
        return True
    return _role(user, project) in _write_roles()


_LOOKUP = object()


def can_manage_monitor(user, monitor: Monitor, *, role=_LOOKUP) -> bool:
    """Admins manage any monitor, auditors their own. Pass ``role`` when checking many."""
    from accounts.models import ProjectMembership

    if user and user.is_authenticated and user.is_superuser:
        return True
    if role is _LOOKUP:
        role = _role(user, monitor.project)
    if role == ProjectMembership.Role.ADMIN:
        return True
    return role == ProjectMembership.Role.AUDITOR and monitor.created_by_id == user.id


def owner_authorized(monitor: Monitor) -> bool:
    """Whether the creator may still launch runs for this monitor."""
    owner = monitor.created_by
    if owner is None or not owner.is_active:
        return False
    return has_write_role(owner, monitor.project)


def zone(name: str):
    """ZoneInfo for an IANA name; raise ValueError for unknown zones."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {name!r}.") from exc


def cron_next(expr: str, after, tz: str = "UTC"):
    """First firing of ``expr`` on ``tz``'s wall clock strictly after ``after``, in UTC.

    Evaluating in the local zone keeps ``0 6 * * *`` at 06:00 local across
    daylight-saving changes; a firing inside a spring-forward gap runs at the
    first valid moment after it.
    """
    from cronsim import CronSim

    return next(CronSim(expr, after.astimezone(zone(tz)))).astimezone(UTC)


def validate_cron(expr: str, now=None, tz: str = "UTC") -> str:
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
        next(CronSim(expr, now.astimezone(zone(tz))))
    except (CronSimError, StopIteration) as exc:
        raise ValueError(f"Invalid cron expression: {exc or 'never fires'}.") from exc
    return expr


def next_after(monitor: Monitor, now):
    """Next tick strictly after ``now`` for either timing rule."""
    if monitor.cron_expression:
        return cron_next(monitor.cron_expression, now, monitor.timezone)
    return advance(monitor.next_run_at, monitor.interval_hours, now)


def advance(next_run_at, interval_hours: int, now):
    """Next tick strictly after ``now``, on the monitor's grid.

    Missed ticks (worker down for a week) are skipped rather than replayed, so a
    recovering worker launches one run, not a burst of stale ones.
    """
    step = timedelta(hours=max(interval_hours, 1))
    if next_run_at > now:
        return next_run_at
    missed = int((now - next_run_at) / step) + 1
    return next_run_at + missed * step


def resolve_version(monitor: Monitor):
    if monitor.scenario_set_version_id:
        return monitor.scenario_set_version
    return monitor.scenario_set.versions.order_by("-version").first()


def resolve_judge(monitor: Monitor):
    """The judge version the next tick uses: pinned, or the judge's newest."""
    return monitor.judge_version if monitor.judge_version_id else monitor.judge.latest


def launch_monitor(monitor: Monitor, *, now=None) -> AuditRun:
    """Create (not submit) one frozen AuditRun for ``monitor``."""
    from audits.services import create_audit_run

    now = now or timezone.now()
    version = resolve_version(monitor)
    if version is None:
        raise ValueError("Scenario set has no published version.")
    if monitor.created_by is None:
        raise ValueError("Monitor owner no longer exists; recreate the monitor.")
    params = dict(monitor.generation_parameters or {})
    overrides = {k: params.pop(k, None) for k in _FORM_KEYS}
    return create_audit_run(
        project=monitor.project,
        user=monitor.created_by,
        name=f"{monitor.name} · {now:%Y-%m-%d %H:%M}",
        scenario_set_version=version,
        target_model=monitor.target_model,
        auditor_model=monitor.auditor_model,
        judge=resolve_judge(monitor),
        max_turns_override=overrides["max_turns"],
        language_override=overrides["language"],
        n_repetitions_override=overrides["n_repetitions"],
        gen_config_override=params or None,
        monitor=monitor,
        experiment=monitor.experiment,
    )


# ─── Creating monitors (from New Experiment's "Repeat") ──────────────────────

# Repeat choices offered on New Experiment: (value, label). "once" = no monitor.
REPEAT_CHOICES = [
    ("once", "Once"),
    ("6", "Every 6 hours"),
    ("24", "Daily"),
    ("72", "Every 3 days"),
    ("168", "Weekly"),
    ("336", "Every 2 weeks"),
    ("cron", "Custom (cron expression)…"),
]
START_CHOICES = ("now", "at", "baseline")


def parse_repeat(post, now=None) -> dict | None:
    """Validate New Experiment's Repeat fields. None means "Once".

    start: "now" (run immediately, then repeat), "at" (first run at a chosen
    time, nothing runs now) or "baseline" (use an existing run as the first
    point, repeat from the next tick). Raises ValueError with a user message.
    """
    from datetime import datetime

    choice = (post.get("repeat") or "once").strip()
    if choice == "once":
        return None
    now = now or timezone.now()
    tz_name = (post.get("timezone") or "UTC").strip()
    tz = zone(tz_name)
    cron_expression, interval_hours = "", 0
    if choice == "cron":
        cron_expression = validate_cron(post.get("cron_expression") or "", tz=tz_name)
    else:
        try:
            interval_hours = int(choice)
        except ValueError as exc:
            raise ValueError("Pick how often to repeat.") from exc
        if not MIN_INTERVAL_HOURS <= interval_hours <= MAX_INTERVAL_HOURS:
            raise ValueError(f"Repeat interval must be between {MIN_INTERVAL_HOURS} hours and 90 days.")
    start = (post.get("start") or "now").strip()
    if start not in START_CHOICES:
        start = "now"
    if start == "at":
        raw = (post.get("first_run_at") or "").strip()
        if not raw:
            raise ValueError("Pick when the first run should start, or choose “Now”.")
        at = timezone.make_aware(datetime.fromisoformat(raw), tz)
        if at <= now:
            raise ValueError("The first run time is in the past.")
        first = cron_next(cron_expression, at - timedelta(minutes=1), tz_name) if cron_expression else at
    elif cron_expression:
        first = cron_next(cron_expression, now, tz_name)
    else:
        first = now + timedelta(hours=interval_hours)
    return {
        "choice": choice,
        "interval_hours": interval_hours,
        "cron_expression": cron_expression,
        "timezone": tz_name,
        "start": start,
        "first_run_at": first,
        "baseline_run": (post.get("clone_from") or "").strip() if start == "baseline" else "",
    }


def repeat_label(repeat: dict | None) -> str:
    if not repeat:
        return "once"
    if repeat["cron_expression"]:
        return f"cron {repeat['cron_expression']} ({repeat['timezone']})"
    return Monitor(interval_hours=repeat["interval_hours"]).interval_display


def run_matches_monitor(run: AuditRun, monitor: Monitor) -> bool:
    """Whether ``run`` is the same experiment setup the monitor will repeat."""
    version = resolve_version(monitor)
    return (
        version is not None
        and run.scenario_set_version_id == version.id
        and run.target_model_id == monitor.target_model_id
        and run.auditor_model_id == monitor.auditor_model_id
        and run.judge_version_id == getattr(resolve_judge(monitor), "pk", None)
        and (run.generation_parameters_snapshot or {}) == (monitor.generation_parameters or {})
    )


def create_monitor(*, project, user, name: str, run: dict, repeat: dict, experiment=None, first_point=None) -> Monitor:
    """A monitor repeating one run setup (``run``: the launch dict used for runs).

    ``first_point``: an already-launched run of the same setup; it becomes the
    first point of the drift series (and blocks the next tick while active).
    """
    from accounts.models import ProjectMembership
    from audits.services import _generation_parameters
    from scenarios.services import require_project_role

    # Monitors spend the workspace's keys unattended: same rule as launching.
    require_project_role(user, project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    if Monitor.objects.filter(project=project).count() >= MAX_MONITORS_PER_PROJECT:
        raise ValueError(
            f"This workspace already has {MAX_MONITORS_PER_PROJECT} monitors (the limit). Delete some first."
        )
    monitor = Monitor.objects.create(
        project=project,
        name=name[:250],
        scenario_set=run["version"].scenario_set,
        # Pinned unless "Always latest" was picked (then each tick resolves the
        # newest version; drift baselines reset when the version changes).
        scenario_set_version=None if getattr(run["version"], "follow_latest", False) else run["version"],
        target_model=run["target"],
        auditor_model=run["auditor"],
        judge=run["judge"].judge,
        # Pinned unless the judge was picked as "Always latest".
        judge_version=None if getattr(run["judge"], "follow_latest", False) else run["judge"],
        generation_parameters=_generation_parameters(
            max_turns_override=run["max_turns"],
            language_override=run["language"],
            n_repetitions_override=run["n_repetitions"],
            gen_config_override=run["gen_config"],
        ),
        interval_hours=repeat["interval_hours"],
        cron_expression=repeat["cron_expression"],
        timezone=repeat["timezone"],
        next_run_at=repeat["first_run_at"],
        experiment=experiment,
        created_by=user,
    )
    if first_point is not None and run_matches_monitor(first_point, monitor):
        AuditRun.objects.filter(pk=first_point.pk).update(monitor=monitor)
        monitor.last_run = first_point
        monitor.save(update_fields=["last_run"])
    return monitor


def run_due_monitors(now=None) -> list[int]:
    """Launch every enabled monitor whose ``next_run_at`` has passed.

    Returns the ids of the runs created. A tick whose previous run is still in
    flight is skipped (never overlap runs of one series); a tick that fails to
    create its run records ``last_error`` and still advances, so one broken
    monitor cannot wedge the loop. A monitor whose creator lost the
    admin/auditor role is paused instead of launched.
    """
    from audits.services import submit_audit_run

    now = now or timezone.now()
    created: list[AuditRun] = []
    with transaction.atomic():
        due = (
            Monitor.objects.select_for_update(skip_locked=True)
            .filter(enabled=True, next_run_at__lte=now)
            .select_related("last_run", "project", "created_by")
            .order_by("next_run_at")
        )
        for monitor in due:
            monitor.next_run_at = next_after(monitor, now)
            monitor.last_tick_at = now
            last = monitor.last_run
            if not owner_authorized(monitor):
                monitor.enabled = False
                who = monitor.created_by.username if monitor.created_by else "deleted user"
                monitor.last_error = (
                    f"Paused {now:%Y-%m-%d %H:%M}: owner {who} no longer has admin or auditor role "
                    "in this workspace. Recreate the monitor under an authorized user."
                )
                logger.warning("Monitor %s paused: owner lost write role", monitor.pk)
            elif last is not None and last.is_active:
                monitor.last_error = f"Skipped {now:%Y-%m-%d %H:%M}: run #{last.id} still {last.status}."
            else:
                try:
                    run = launch_monitor(monitor, now=now)
                except Exception as exc:  # noqa: BLE001 - recorded on the monitor, loop continues
                    monitor.last_error = f"{now:%Y-%m-%d %H:%M}: {exc}"
                    logger.warning("Monitor %s tick failed: %s", monitor.pk, exc)
                else:
                    monitor.last_run = run
                    monitor.last_error = ""
                    created.append(run)
            monitor.save(
                update_fields=["enabled", "next_run_at", "last_tick_at", "last_run", "last_error", "updated_at"]
            )

    for run in created:
        try:
            submit_audit_run(run)
        except Exception as exc:  # noqa: BLE001 - run stays queued; the sweeper retries submission
            logger.warning("Scheduled run %s not submitted: %s", run.pk, exc)
    if created:
        logger.info("Monitors launched %d run(s): %s", len(created), [r.pk for r in created])
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


def drift_series(monitor: Monitor) -> list[dict]:
    """Chronological points for the monitor's runs with CI and change flags.

    Each completed run is tested against the pooled previous ``BASELINE_WINDOW``
    completed runs on the same scenario set and judge versions; ``change`` is
    "drop"/"rise" when |z| >= 1.96, else "".
    """
    runs = list(
        monitor.runs.filter(archived=False)
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
            "judge_version": run.judge_version_id,
            # Other scenarios or another judge version: not comparable with before.
            "version_changed": bool(points) and (
                points[-1]["version"] != run.scenario_set_version.version
                or points[-1]["judge_version"] != run.judge_version_id
            ),
        }
        if c["n"]:
            point["lo"], point["hi"] = wilson(c["k"], c["n"])
        if point["version_changed"]:
            # Different scenarios or judge: the old baseline is not comparable.
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
