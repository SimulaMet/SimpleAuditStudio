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


def _models(project):
    """Models a judge can use, for the picker (grouped by connection in the template)."""
    from model_registry.models import RegisteredModel

    return list(
        RegisteredModel.objects.filter(project=project, enabled=True, connection__enabled=True)
        .select_related("connection").order_by("connection__name", "display_name")
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
                Prefetch("versions", queryset=JudgeVersion.objects.select_related("model__connection").order_by("-version"))
            )
        )
        usage = judge_usage_counts(p)
        for judge in judges:
            versions = list(judge.versions.all())
            judge.current = _decorate(versions[0]) if versions else None
            judge.usage = usage.get(judge.id, 0)
        kw.update(judges=judges, models=_models(p), rubrics=rubric_choices())
        return super().get_context_data(**kw)

    def post(self, request):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        p = request.project
        action = request.POST.get("action")
        if action == "starters":
            from model_registry.models import RegisteredModel

            model = RegisteredModel.objects.filter(pk=request.POST.get("model_id") or 0, project=p).first()
            if model is None:
                messages.error(request, "Pick a model for the judges.")
                return redirect("judges")
            made = ensure_starter_judges(p, model, request.user)
            messages.success(request, f"Created {len(made)} judge{'s' if len(made) != 1 else ''} using {model.display_name}.")
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

    Saving name / description updates the judge; saving model, rubric or
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
        p = self.request.project
        judge = self._judge()
        versions, shown = [], None
        if judge is not None:
            versions = [
                _decorate(v) for v in judge.versions.select_related("model__connection", "created_by")
                .annotate(run_count=Count("audit_runs")).order_by("-version")
            ]
            wanted = self.request.GET.get("v", "")
            shown = next((v for v in versions if str(v.version) == wanted), versions[0] if versions else None)
        # Form values: a re-shown POST, the shown version, or a new judge from ?rubric=.
        form = kw.pop("form", None)
        if form is None:
            if shown is not None:
                form = {
                    "name": judge.name, "description": judge.description, "model_id": shown.model_id,
                    "rubric": shown.rubric, "probe_prompt": shown.effective_probe, "judge_prompt": shown.effective_judge,
                }
            else:
                start = rubric(self.request.GET.get("rubric", "safety"))
                models = _models(p)
                form = {
                    "name": "", "description": start["description"], "model_id": models[0].pk if models else None,
                    "rubric": start["key"], "probe_prompt": start["probe_prompt"], "judge_prompt": start["judge_prompt"],
                }
        kw.update(
            judge=judge,
            versions=versions,
            shown=shown,
            latest=versions[0] if versions else None,
            form=form,
            models=_models(p),
            rubrics=rubric_choices(),
            monitors=list(judge.monitors.select_related("judge_version")) if judge else [],
        )
        return super().get_context_data(**kw)

    def post(self, request, judge_id=None):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        from model_registry.models import RegisteredModel

        p = request.project
        judge = self._judge()
        post = request.POST
        form = {k: post.get(k, "") for k in ("name", "description", "model_id", "rubric", "probe_prompt", "judge_prompt", "note")}
        name = form["name"].strip()
        model = RegisteredModel.objects.filter(pk=form["model_id"] or 0, project=p).first()
        error = None
        if not name:
            error = "Give the judge a name."
        elif Judge.objects.filter(project=p, name=name).exclude(pk=getattr(judge, "pk", None)).exists():
            error = f"A judge named “{name}” already exists."
        elif model is None:
            error = "Pick the model that grades."
        if error is None:
            try:
                if judge is None:
                    judge = create_judge(
                        project=p, name=name, description=form["description"], model=model, rubric=form["rubric"],
                        probe_prompt=form["probe_prompt"], judge_prompt=form["judge_prompt"],
                        note=form["note"] or "Created", user=request.user,
                    )
                    messages.success(request, f"Judge “{judge.name}” created.")
                    return redirect("judge_detail", judge.id)
                judge.name, judge.description = name, form["description"].strip()
                judge.save(update_fields=["name", "description", "updated_at"])
                version, created = save_version(
                    judge, model=model, rubric=form["rubric"], probe_prompt=form["probe_prompt"],
                    judge_prompt=form["judge_prompt"], note=form["note"], user=request.user,
                )
            except ValueError as e:
                error = str(e)
            else:
                messages.success(request, f"Saved {version.label}." if created else "Saved. Grading setup unchanged, so no new version.")
                return redirect(f"/judges/{judge.id}/")
        messages.error(request, error)
        return self.render_to_response(self.get_context_data(form=form))
