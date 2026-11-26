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
    from judges.services import label_scale

    judge = (run.judge_config_snapshot or {}).get("judge") or {}
    return engine.label_scale(label_scale(judge.get("spec") or {}), judge.get("criteria") or "")


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
