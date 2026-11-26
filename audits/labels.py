"""Human labels: a reviewer's own verdict and comment on a judged transcript."""
from __future__ import annotations

from django.db import transaction

from accounts.models import ProjectMembership
from audits.events import ScenarioResult
from audits.models import AuditRun, HumanLabel
from infra.exceptions import StableAPIError
from scenarios.services import require_project_role

COMMENT_MAX = 5000
# SimpleAudit's word for "looked, could not decide", an answer on every scale.
UNSURE = "unsure"


def run_scale(run: AuditRun) -> dict:
    """The scale ``run``'s judge is checked on (``infra.engine.label_scale``).

    A pass/fail judge is labelled on its own levels, any other judge on the
    severity ladder. The rules shown to a reviewer use the criteria the run
    froze, so they are the ones its judge was given.
    """
    from infra import engine

    judge = (run.judge_config_snapshot or {}).get("judge") or {}
    return engine.label_scale(_scale_name(run), judge.get("criteria") or "")


def _scale_name(run: AuditRun) -> str:
    from judges.services import label_scale

    return label_scale(((run.judge_config_snapshot or {}).get("judge") or {}).get("spec") or {})


def transcript_run_id(run_id: int) -> int:
    """The run that produced the transcripts ``run_id`` grades.

    A rejudged run grades its source's transcripts
    (``runtime_metadata["rejudged_from"]``), also over a rejudge of a rejudge.
    Any other run is its own.
    """
    seen = {run_id}
    while True:
        metadata = AuditRun.objects.filter(pk=run_id).values_list("runtime_metadata", flat=True).first()
        source = ((metadata or {}).get("rejudged_from") or {}).get("run_id")
        if not source or source in seen:
            return run_id
        seen.add(source)
        run_id = source


def origin_result(result: ScenarioResult) -> ScenarioResult:
    """The result that first held ``result``'s transcript: itself, or for a
    rejudged run the same scenario's result in the run it leads back to."""
    source_run_id = transcript_run_id(result.run_id)
    if source_run_id == result.run_id:
        return result
    source = ScenarioResult.objects.filter(run_id=source_run_id, version_item_id=result.version_item_id).first()
    return source or result


def labels_by_rep(result: ScenarioResult) -> dict[int, list[HumanLabel]]:
    """The labels on ``result``'s transcripts, on every scale, per repetition number, oldest first."""
    by_rep: dict[int, list[HumanLabel]] = {}
    for label in HumanLabel.objects.filter(result=origin_result(result)).select_related("created_by"):
        by_rep.setdefault(label.rep, []).append(label)
    return by_rep


@transaction.atomic
def set_label(*, result: ScenarioResult, rep: int, user, verdict: str, comment: str = "") -> HumanLabel | None:
    """Save ``user``'s label for one transcript, replacing their earlier one.

    The label is on the scale of the run ``result`` belongs to (``run_scale``).
    A blank ``verdict`` removes it and returns ``None``. One label per
    reviewer, transcript and scale, so several reviewers can label the same
    result, and a label given under a judge on another scale is kept.
    """
    run = AuditRun.objects.select_related("project").get(pk=result.run_id)
    require_project_role(user, run.project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    reps = (result.result or {}).get("reps")
    if not 1 <= rep <= (len(reps) if isinstance(reps, list) else 1):
        raise StableAPIError(detail="This result has no such repetition.", code="invalid_label")
    scale = run_scale(run)
    mine = {"result": origin_result(result), "rep": rep, "scale": scale["name"], "created_by": user}
    if not verdict:
        HumanLabel.objects.filter(**mine).delete()
        return None
    answers = [*scale["levels"], UNSURE]
    if verdict not in answers:
        raise StableAPIError(detail=f"Label must be one of {', '.join(answers)}.", code="invalid_label")
    label, _ = HumanLabel.objects.update_or_create(
        **mine, defaults={"verdict": verdict, "comment": comment.strip()[:COMMENT_MAX]},
    )
    return label


def _verdicts(run_id: int) -> dict[str, dict]:
    """Each judged transcript of a run and what the judge stored for it:
    ``severity``, and a pass/fail judge's ``verdict`` and ``verdict_scale``.

    Keyed ``"<version item id>#<repetition number>"``, as the labels are, and
    read from ``list_summary`` so no transcript is loaded.
    """
    verdicts = {}
    rows = ScenarioResult.objects.filter(run_id=run_id).values_list("version_item_id", "list_summary")
    for item_id, summary in rows:
        summary = summary or {}
        trials = summary.get("reps") if summary.get("has_reps") else [summary]
        for number, trial in enumerate(trials or [], start=1):
            verdicts[f"{item_id}#{number}"] = trial
    return verdicts


def _same_transcript_runs(project_id: int, source_run_id: int) -> list[AuditRun]:
    """The run that produced the transcripts and every rejudge of it, oldest first."""
    found, frontier = [source_run_id], [source_run_id]
    while frontier:
        frontier = list(
            AuditRun.objects.filter(project_id=project_id, runtime_metadata__rejudged_from__run_id__in=frontier)
            .exclude(pk__in=found).values_list("pk", flat=True)
        )
        found += frontier
    return list(AuditRun.objects.filter(pk__in=found).order_by("id"))


def calibration(run: AuditRun) -> dict | None:
    """How each judge tried on ``run``'s transcripts agrees with their human labels.

    The run that produced the transcripts and every rejudge of it graded the
    same conversations, so each gets a row against the same labels: label
    once, rejudge with another judge, and compare. Everything is compared on
    the scale of ``run``'s own judge (``run_scale``).

    Returns what ``infra.engine.calibrate_judges`` does, with ``scale`` (the
    scale's name) and ``rows``. A row is a judge's agreement plus its ``run``
    and the ``scale`` that judge grades on; the agreement is missing when its
    verdicts (or every label) cannot be read on the page's scale. ``None``
    when no transcript is labelled yet.
    """
    from infra import engine

    source_run_id = transcript_run_id(run.id)
    raters: dict[int | None, list[tuple[str, str, str]]] = {}
    for label in HumanLabel.objects.filter(result__run_id=source_run_id).select_related("result"):
        item = f"{label.result.version_item_id}#{label.rep}"
        raters.setdefault(label.created_by_id, []).append((item, label.verdict, label.scale))
    if not raters:
        return None
    members = _same_transcript_runs(run.project_id, source_run_id)
    scale = _scale_name(run)
    report = engine.calibrate_judges({member.id: _verdicts(member.id) for member in members}, list(raters.values()), scale)
    rows = [
        {"run": member, "scale": _scale_name(member), **(report["judges"].get(member.id) or {})}
        for member in members
    ]
    return {**report, "scale": scale, "rows": rows}
