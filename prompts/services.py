"""System prompt versioning: create, save (no-op if unchanged), delete, clone."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from prompts.models import SystemPrompt, SystemPromptVersion


def save_version(prompt: SystemPrompt, *, content: str, note: str = "", user=None) -> tuple[SystemPromptVersion, bool]:
    """Save a new version when the content changed.

    Returns ``(version, created)``; unchanged content returns the latest version
    with ``created=False``.
    """
    with transaction.atomic():
        SystemPrompt.objects.select_for_update().filter(pk=prompt.pk).first()
        latest = prompt.versions.order_by("-version").first()
        if latest is not None and latest.content == content:
            return latest, False
        number = (prompt.versions.aggregate(n=Max("version"))["n"] or 0) + 1
        version = SystemPromptVersion.objects.create(
            prompt=prompt, version=number, content=content, note=note.strip()[:500], created_by=user,
        )
    return version, True


def create_prompt(*, project, name: str, description: str = "", content: str = "", user=None, note: str = "") -> SystemPrompt:
    """A new system prompt with its v1."""
    name = name.strip()
    if not name:
        raise ValueError("Prompt name is required.")
    if SystemPrompt.objects.filter(project=project, name=name).exists():
        raise ValueError(f"A system prompt named \u201c{name}\u201d already exists.")
    with transaction.atomic():
        prompt = SystemPrompt.objects.create(
            project=project, name=name[:250], description=description.strip(), created_by=user,
        )
        save_version(prompt, content=content, note=note or "Created", user=user)
    return prompt


def unique_name(project, name: str) -> str:
    """``name``, or ``name (2)``, ``name (3)``\u2026 when taken."""
    taken = set(SystemPrompt.objects.filter(project=project, name__startswith=name).values_list("name", flat=True))
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


def delete_version(version: SystemPromptVersion) -> None:
    """Delete a version no run uses. The only version can't be deleted.

    Raises ``ValueError`` with the reason otherwise.
    """
    from django.db.models import RestrictedError

    from audits.models import AuditRun

    # A version is kept when a run froze its exact text (in the agent config
    # snapshot or the generation parameters), so history stays reproducible.
    used = AuditRun.objects.filter(project=version.prompt.project).exclude(id__isnull=True)
    for run in used.iterator(chunk_size=200):
        snap = run.agent_config_snapshot
        if isinstance(snap, dict) and snap.get("system_prompt") == version.content:
            raise ValueError(
                f"{version.label} matches a run's frozen configuration, so it is kept for reproducibility."
            )
        gen = run.generation_parameters_snapshot
        if isinstance(gen, dict) and gen.get("system_prompt") == version.content:
            raise ValueError(
                f"{version.label} matches a run's generation parameters, so it is kept for reproducibility."
            )

    if version.prompt.versions.count() == 1:
        raise ValueError(f"{version.label} is the prompt's only version: delete the prompt instead.")
    try:
        version.delete()
    except RestrictedError as e:
        raise ValueError(f"{version.label} is in use, so it is kept.") from e


def delete_prompt(prompt: SystemPrompt) -> None:
    """Delete a prompt and all its versions.

    Blocked while anything references it, so history stays reproducible:
    - an audit run froze one of its versions (``system_prompt_version`` FK), or
    - an agent's instructions match one of its version texts.

    Raises ``ValueError`` with the reason when it can't be deleted.
    """
    from audits.models import AuditRun
    from model_registry.models import Agent

    # A run pins the exact version it used; deleting the prompt would break that.
    if AuditRun.objects.filter(system_prompt_version__prompt=prompt).exists():
        raise ValueError(
            f"\u201c{prompt.name}\u201d is pinned by one or more audit runs, so it is kept for reproducibility."
        )
    # Agents store raw text; block when their instructions match any version here.
    contents = list(prompt.versions.values_list("content", flat=True))
    if contents and Agent.objects.filter(project=prompt.project, system_prompt__in=contents).exists():
        raise ValueError(
            f"An agent still uses \u201c{prompt.name}\u201d's text. Detach it from that agent first."
        )
    prompt.delete()
