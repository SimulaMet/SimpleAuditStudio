"""System prompt pages: list, create, view / edit (saves a new version), delete."""
from itertools import pairwise

from django.contrib import messages
from django.db.models import Count, Prefetch
from django.http import Http404
from django.shortcuts import redirect
from django.views.generic import TemplateView

from infra.ui import ProjectMixin, _require_write_access
from prompts.models import SystemPrompt, SystemPromptVersion
from prompts.services import (
    create_prompt,
    delete_prompt,
    delete_version,
    save_version,
    unique_name,
)


def _diff_entry(version: SystemPromptVersion) -> dict:
    """One version as a field for VersionDiff.fields()."""
    return {
        "version": version.version,
        "note": version.note,
        "created": version.created_at.strftime("%b %-d, %Y, %H:%M"),
        "fields": [
            {"label": "Note", "value": version.note or ""},
            {"label": "Prompt content", "value": version.content},
        ],
    }


class PromptsView(ProjectMixin, TemplateView):
    """List all system prompts in the current workspace."""

    template_name = "prompts.html"

    def get_context_data(self, **kw):
        p = self.request.project
        prompts = list(
            SystemPrompt.objects.filter(project=p).order_by("name").annotate(
                version_count=Count("versions", distinct=True)
            ).prefetch_related(
                Prefetch("versions", queryset=SystemPromptVersion.objects.order_by("-version"))
            )
        )
        for prompt in prompts:
            versions = list(prompt.versions.all())
            prompt.current = versions[0] if versions else None
        kw.update(prompts=prompts)
        return super().get_context_data(**kw)

    def post(self, request):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        p = request.project
        action = request.POST.get("action")
        if action == "delete_version":
            version = SystemPromptVersion.objects.select_related("prompt").filter(
                pk=request.POST.get("version_id") or 0, prompt__project=p).first()
            if version is None:
                messages.error(request, "Version not found.")
                return redirect("prompts")
            try:
                delete_version(version)
                messages.success(request, f"Deleted {version.label}.")
            except ValueError as e:
                messages.error(request, str(e))
            return redirect("prompt_detail", prompt_id=version.prompt_id)
        elif action == "delete":
            prompt = SystemPrompt.objects.filter(pk=request.POST.get("prompt_id") or 0, project=p).first()
            if prompt is not None:
                try:
                    delete_prompt(prompt)
                    messages.success(request, f"Deleted system prompt \u201c{prompt.name}\u201d.")
                except ValueError as e:
                    messages.error(request, str(e))
        return redirect("prompts")


class PromptDetailView(ProjectMixin, TemplateView):
    """Create (no prompt_id) or view / edit a system prompt. ``?v=N`` shows version N.

    Saving the content creates a new version; name/description update in place.
    Each version carries a note (commit message) explaining the change.
    """

    template_name = "prompt_detail.html"

    def _prompt(self):
        prompt_id = self.kwargs.get("prompt_id")
        if prompt_id is None:
            return None
        prompt = SystemPrompt.objects.filter(pk=prompt_id, project=self.request.project).first()
        if prompt is None:
            raise Http404("System prompt not found in this workspace.")
        return prompt

    def get_context_data(self, **kw):
        prompt = self._prompt()
        versions, shown = [], None
        if prompt is not None:
            versions = list(prompt.versions.select_related("created_by").order_by("-version"))
            for newer, older in pairwise(versions):
                newer.previous_number = older.version
            wanted = self.request.GET.get("v", "")
            shown = next((v for v in versions if str(v.version) == wanted), versions[0] if versions else None)

        form = kw.pop("form", None)
        cloning = None
        if prompt is None:
            cloning = SystemPromptVersion.objects.select_related("prompt").filter(
                pk=self.request.GET.get("clone") or (form or {}).get("clone") or 0,
                prompt__project=self.request.project,
            ).first()
        if form is None:
            if shown is not None:
                form = {
                    "name": prompt.name, "description": prompt.description,
                    "content": shown.content, "note": "",
                }
            elif cloning is not None:
                src = cloning.prompt
                form = {
                    "name": unique_name(src.project, f"{src.name} copy"),
                    "description": src.description,
                    "content": cloning.content,
                    "note": f"Cloned from {cloning.label}",
                }
            else:
                form = {"name": "", "description": "", "content": "", "note": ""}

        kw.update(
            prompt=prompt,
            versions=versions,
            shown=shown,
            latest=versions[0] if versions else None,
            shown_is_latest=bool(versions) and shown == versions[0],
            cloning=cloning,
            diff_versions=[_diff_entry(v) for v in versions],
            previous=next((v for v in versions if shown and v.version < shown.version), None),
            form=form,
        )
        return super().get_context_data(**kw)

    def post(self, request, prompt_id=None):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        p = request.project
        prompt = self._prompt()
        post = request.POST
        form = {k: post.get(k, "") for k in ("name", "description", "content", "note")}
        name = form["name"].strip()
        error = None
        if not name:
            error = "Give the prompt a name."
        elif SystemPrompt.objects.filter(project=p, name=name).exclude(pk=getattr(prompt, "pk", None)).exists():
            error = f"A system prompt named \u201c{name}\u201d already exists."
        if error is None:
            try:
                if prompt is None:
                    prompt = create_prompt(
                        project=p, name=name, description=form["description"],
                        content=form["content"], user=request.user, note=form["note"] or "Created",
                    )
                    messages.success(request, f"Created {prompt.latest.label}.")
                else:
                    if name != prompt.name or form["description"] != prompt.description:
                        prompt.name = name
                        prompt.description = form["description"]
                        prompt.save(update_fields=["name", "description", "updated_at"])
                    version, created = save_version(
                        prompt, content=form["content"], note=form["note"], user=request.user,
                    )
                    messages.success(
                        request,
                        f"Saved {version.label}." if created
                        else "Saved. Content unchanged, so no new version.",
                    )
            except ValueError as e:
                error = str(e)
            else:
                return redirect(f"/prompts/{prompt.id}/")
        messages.error(request, error)
        return self.render_to_response(self.get_context_data(form=form))
