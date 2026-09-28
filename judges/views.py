"""Judges pages: list, create, view / edit (saves a new version), clone, delete."""
from django.contrib import messages
from django.db.models import Count, Prefetch, ProtectedError, RestrictedError
from django.http import Http404
from django.shortcuts import redirect
from django.views.generic import TemplateView

from infra.ui import ProjectMixin, _require_write_access
from judges.models import Judge, JudgeVersion
from judges.services import (
    OUTPUT_LABELS,
    clone_judge,
    create_judge,
    ensure_starter_judges,
    judge_usage_counts,
    rubric,
    rubric_choices,
    save_version,
)


def _decorate(version: JudgeVersion) -> JudgeVersion:
    info = rubric(version.rubric)
    version.rubric_name = info["name"]
    version.output_label = OUTPUT_LABELS[info["output"]]
    version.effective_probe = version.probe_prompt or info["probe_prompt"]
    version.effective_judge = version.judge_prompt or info["judge_prompt"]
    return version


class JudgesView(ProjectMixin, TemplateView):
    template_name = "judges.html"

    def get_context_data(self, **kw):
        p = self.request.project
        judges = list(
            Judge.objects.filter(project=p).order_by("name").annotate(
                version_count=Count("versions", distinct=True), monitor_count=Count("monitors", distinct=True)
            ).prefetch_related(
                Prefetch("versions", queryset=JudgeVersion.objects.order_by("-version"))
            )
        )
        usage = judge_usage_counts(p)
        for judge in judges:
            versions = list(judge.versions.all())
            judge.current = _decorate(versions[0]) if versions else None
            judge.usage = usage.get(judge.id, 0)
        used = set(JudgeVersion.objects.filter(judge__project=p).values_list("rubric", flat=True))
        kw.update(judges=judges, rubrics=rubric_choices(),
                  missing_rubrics=[r for r in rubric_choices() if r["key"] not in used])
        return super().get_context_data(**kw)

    def post(self, request):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        p = request.project
        action = request.POST.get("action")
        if action == "starters":
            made = ensure_starter_judges(p, request.user)
            messages.success(request, f"Added {len(made)} SimpleAudit judge{'s' if len(made) != 1 else ''}.")
        elif action == "clone":
            version = JudgeVersion.objects.select_related("judge").filter(
                pk=request.POST.get("version_id") or 0, judge__project=p
            ).first()
            if version is None:
                messages.error(request, "Judge not found.")
                return redirect("judges")
            judge = clone_judge(version, user=request.user)
            messages.success(request, f"Cloned {version.label} as “{judge.name}”. Edit it below.")
            return redirect("judge_detail", judge.id)
        elif action == "delete":
            judge = Judge.objects.filter(pk=request.POST.get("judge_id") or 0, project=p).first()
            if judge is not None:
                try:
                    judge.delete()
                    messages.success(request, f"Deleted judge “{judge.name}”.")
                except (ProtectedError, RestrictedError):
                    messages.error(request, f"Can't delete “{judge.name}”: runs or monitors use it. Kept for reproducibility.")
        return redirect("judges")


class JudgeDetailView(ProjectMixin, TemplateView):
    """Create (no judge_id) or view / edit a judge. ``?v=N`` shows version N.

    Saving name / description updates the judge; saving rubric or
    prompts creates a new version (runs keep the version they used).
    """

    template_name = "judge_detail.html"

    def _judge(self):
        judge_id = self.kwargs.get("judge_id")
        if judge_id is None:
            return None
        judge = Judge.objects.filter(pk=judge_id, project=self.request.project).first()
        if judge is None:
            raise Http404("Judge not found in this workspace.")
        return judge

    def get_context_data(self, **kw):
        judge = self._judge()
        versions, shown = [], None
        if judge is not None:
            versions = [
                _decorate(v) for v in judge.versions.select_related("created_by")
                .annotate(run_count=Count("audit_runs")).order_by("-version")
            ]
            wanted = self.request.GET.get("v", "")
            shown = next((v for v in versions if str(v.version) == wanted), versions[0] if versions else None)
        # Form values: a re-shown POST, the shown version, or a new judge from ?rubric=.
        form = kw.pop("form", None)
        if form is None:
            if shown is not None:
                form = {
                    "name": judge.name, "description": judge.description,
                    "rubric": shown.rubric, "probe_prompt": shown.effective_probe, "judge_prompt": shown.effective_judge,
                }
            else:
                start = rubric(self.request.GET.get("rubric", "safety"))
                form = {
                    "name": "", "description": start["description"],
                    "rubric": start["key"], "probe_prompt": start["probe_prompt"], "judge_prompt": start["judge_prompt"],
                }
        kw.update(
            judge=judge,
            versions=versions,
            shown=shown,
            latest=versions[0] if versions else None,
            form=form,
            rubrics=rubric_choices(form.get("rubric")),
            monitors=list(judge.monitors.select_related("judge_version")) if judge else [],
        )
        return super().get_context_data(**kw)

    def post(self, request, judge_id=None):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        p = request.project
        judge = self._judge()
        post = request.POST
        form = {k: post.get(k, "") for k in ("name", "description", "rubric", "probe_prompt", "judge_prompt", "note")}
        name = form["name"].strip()
        error = None
        if not name:
            error = "Give the judge a name."
        elif Judge.objects.filter(project=p, name=name).exclude(pk=getattr(judge, "pk", None)).exists():
            error = f"A judge named “{name}” already exists."
        if error is None:
            try:
                if judge is None:
                    judge = create_judge(
                        project=p, name=name, description=form["description"], rubric=form["rubric"],
                        probe_prompt=form["probe_prompt"], judge_prompt=form["judge_prompt"],
                        note=form["note"] or "Created", user=request.user,
                    )
                    messages.success(request, f"Judge “{judge.name}” created.")
                    return redirect("judge_detail", judge.id)
                judge.name, judge.description = name, form["description"].strip()
                judge.save(update_fields=["name", "description", "updated_at"])
                version, created = save_version(
                    judge, rubric=form["rubric"], probe_prompt=form["probe_prompt"],
                    judge_prompt=form["judge_prompt"], note=form["note"], user=request.user,
                )
            except ValueError as e:
                error = str(e)
            else:
                messages.success(request, f"Saved {version.label}." if created else "Saved. Grading setup unchanged, so no new version.")
                return redirect(f"/judges/{judge.id}/")
        messages.error(request, error)
        return self.render_to_response(self.get_context_data(form=form))
