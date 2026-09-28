"""Judges pages: list, create, view / edit (saves a new version), clone, delete."""
from itertools import pairwise

from django.contrib import messages
from django.db.models import Count, Prefetch, ProtectedError, RestrictedError
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from infra.ui import ProjectMixin, _require_write_access
from judges.models import Judge, JudgeVersion
from judges.services import (
    DEFAULT_BASE,
    DEFAULT_JUDGE_BASE,
    FORMATS,
    base,
    base_choices,
    create_judge,
    decorate,
    default_probe_prompt,
    delete_version,
    ensure_starter_judges,
    form_start,
    judge_usage_counts,
    make_spec,
    parse_start,
    resolve,
    save_version,
    unique_name,
)

# Judge form fields that make up the spec (besides "start").
_SPEC_FIELDS = ("criteria", "probe_prompt", "dimensions", "question", "pass_when")


def _version_form(name: str, description: str, version: JudgeVersion) -> dict:
    """Judge form values showing ``version`` (decorated), texts in full so they can be edited."""
    return {
        "name": name, "description": description, "start": form_start(version),
        "criteria": version.resolved["criteria"], "probe_prompt": version.resolved["probe_prompt"],
        "dimensions": "\n".join(version.options.get("dimensions", [])),
        "question": version.options.get("question", ""),
        "pass_when": "yes" if version.options.get("pass_when", True) else "no",
    }


def _diff_entry(version: JudgeVersion) -> dict:
    """One version (decorated) as the fields "What changed" compares (VersionDiff.fields)."""
    info = version.resolved
    options = version.options or {}
    passes = ("yes" if options.get("pass_when", True) else "no") if "question" in options else ""
    return {
        "version": version.version,
        "note": version.note,
        "created": version.created_at.strftime("%b %-d, %Y, %H:%M"),
        "fields": [
            {"label": "Starts from", "value": info["base_name"] or "Own criteria"},
            {"label": "Output format", "value": info["output_label"]},
            {"label": "Score dimensions", "value": options.get("dimensions", [])},
            {"label": "Yes / no question", "value": options.get("question", "")},
            {"label": "Passes when", "value": passes},
            {"label": "Criteria", "value": info["criteria"]},
            {"label": "Probe prompt", "value": info["probe_prompt"]},
            {"label": "Full judge prompt", "value": info["judge_prompt"], "collapsed": True,
             "hint": "criteria + output format, as sent"},
        ],
    }


def _spec_fields(form: dict) -> dict:
    """``make_spec`` arguments from the judge form."""
    return {
        **parse_start(form.get("start", "")),
        "criteria": form.get("criteria", ""),
        "probe_prompt": form.get("probe_prompt", ""),
        "options": {"dimensions": form.get("dimensions", ""), "question": form.get("question", ""),
                    "pass_when": form.get("pass_when", "yes") == "yes"},
    }


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
            judge.current = decorate(versions[0]) if versions else None
            judge.usage = usage.get(judge.id, 0)
        used = set(JudgeVersion.objects.filter(judge__project=p).values_list("base", flat=True))
        kw.update(judges=judges, missing_bases=[b for b in base_choices() if b["key"] not in used])
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
        elif action == "delete_version":
            version = JudgeVersion.objects.select_related("judge").filter(
                pk=request.POST.get("version_id") or 0, judge__project=p).first()
            if version is None:
                messages.error(request, "Version not found.")
                return redirect("judges")
            try:
                delete_version(version)
                messages.success(request, f"Deleted {version.label}.")
            except ValueError as e:
                messages.error(request, str(e))
            return redirect("judge_detail", version.judge_id)
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

    Saving name / description updates the judge; saving the criteria, output
    format or probe prompt creates a new version (runs keep the version they used).
    ``/judges/new/?clone=<version id>`` prefills the new-judge form from that
    version; nothing is saved until the form is submitted.
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
                decorate(v) for v in judge.versions.select_related("created_by")
                .annotate(run_count=Count("audit_runs", distinct=True), monitor_count=Count("monitors", distinct=True))
                .order_by("-version")
            ]
            for newer, older in pairwise(versions):
                newer.previous_number = older.version   # for "Changes"; numbers may have gaps
            wanted = self.request.GET.get("v", "")
            shown = next((v for v in versions if str(v.version) == wanted), versions[0] if versions else None)
        # Form values: a re-shown POST, the shown version, a clone (?clone=), or a
        # new judge from ?base= / ?format=.
        form = kw.pop("form", None)
        cloning = None
        if judge is None:
            cloning = JudgeVersion.objects.select_related("judge").filter(
                pk=self.request.GET.get("clone") or (form or {}).get("clone") or 0,
                judge__project=self.request.project,
            ).first()
            if cloning is not None:
                decorate(cloning)
        if form is None:
            if shown is not None:
                form = _version_form(judge.name, judge.description, shown)
            elif cloning is not None:
                src = cloning.judge
                form = _version_form(unique_name(src.project, f"{src.name} copy"), src.description, cloning)
                form.update(clone=cloning.id, note=f"Cloned from {cloning.label}")
                # A SimpleAudit judge whose output a generic format matches becomes
                # own criteria in that format: same grading, nothing left to write.
                info = base(cloning.base) if cloning.base else {}
                if info.get("equivalent"):
                    form.update(start=f"format:{info['equivalent']}",
                                criteria=cloning.criteria or info["own_criteria"],
                                dimensions="\n".join(info["dimensions"]))
            else:
                wanted = self.request.GET.get("format", "")
                start = form_start(base_key="" if wanted in FORMATS else self.request.GET.get("base") or DEFAULT_JUDGE_BASE,
                                   output=wanted)
                info = base(start) if not start.startswith("format:") else None
                form = {
                    "name": "", "description": info["description"] if info else "", "start": start,
                    "criteria": info["criteria"] if info else "",
                    "probe_prompt": info["probe_prompt"] if info else default_probe_prompt(),
                    "dimensions": "", "question": "", "pass_when": "no",
                }
        current_base = parse_start(form.get("start", ""))["base"]
        kw.update(
            judge=judge,
            versions=versions,
            shown=shown,
            latest=versions[0] if versions else None,
            shown_is_latest=bool(versions) and shown == versions[0],
            judge_in_use=any(v.run_count or v.monitor_count for v in versions)
            or (judge is not None and judge.monitors.exists()),
            cloning=cloning,
            diff_versions=[_diff_entry(v) for v in versions],
            previous=next((v for v in versions if shown and v.version < shown.version), None),
            clone_base=base(cloning.base) if cloning is not None and cloning.base else None,
            start_label=FORMATS.get(form.get("start", "").removeprefix("format:"), {}).get("label", ""),
            form=form,
            bases=base_choices(DEFAULT_BASE if current_base == DEFAULT_BASE else None),
            formats=[{"key": k, **f} for k, f in FORMATS.items()],
            default_probe=default_probe_prompt(),
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
        form = {k: post.get(k, "") for k in ("name", "description", "start", *_SPEC_FIELDS, "note", "clone")}
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
                        project=p, name=name, description=form["description"],
                        note=form["note"] or "Created", user=request.user, **_spec_fields(form),
                    )
                    messages.success(request, f"Judge “{judge.name}” created.")
                    return redirect("judge_detail", judge.id)
                make_spec(**_spec_fields(form))   # validate before renaming
                judge.name, judge.description = name, form["description"].strip()
                judge.save(update_fields=["name", "description", "updated_at"])
                version, created = save_version(judge, note=form["note"], user=request.user, **_spec_fields(form))
            except ValueError as e:
                error = str(e)
            else:
                messages.success(request, f"Saved {version.label}." if created else "Saved. Criteria, format and probe prompt unchanged, so no new version.")
                return redirect(f"/judges/{judge.id}/")
        messages.error(request, error)
        return self.render_to_response(self.get_context_data(form=form))


class JudgePreviewView(ProjectMixin, TemplateView):
    """POST the judge form, get back the full judge prompt it would grade with
    (the live preview on the judge page)."""

    def post(self, request):
        form = {k: request.POST.get(k, "") for k in ("start", *_SPEC_FIELDS)}
        try:
            info = resolve(make_spec(**_spec_fields(form)))
        except ValueError as e:
            return JsonResponse({"error": str(e)})
        return JsonResponse({"judge_prompt": info["judge_prompt"], "format_prompt": info["format_prompt"]})
