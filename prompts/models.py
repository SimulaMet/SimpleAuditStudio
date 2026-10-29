"""Versioned system prompts.

A SystemPrompt is a named, versioned instruction block that can be attached to
an Agent or used as the target's system prompt in an audit run. Each
SystemPromptVersion is immutable: editing saves a new version with a note
(commit message) explaining the change. Runs pin the exact version they used,
so historical runs remain reproducible and comparable.
"""
from django.conf import settings
from django.db import models


class SystemPrompt(models.Model):
    """Identity of a system prompt (name and description are not versioned)."""

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="system_prompts")
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_system_prompt"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_prompt_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name

    @property
    def latest(self) -> "SystemPromptVersion | None":
        return self.versions.order_by("-version").first()


class SystemPromptVersion(models.Model):
    """One immutable version of a system prompt: the text plus a note.

    The note is a commit-message-style explanation of what changed and why,
    making it easy to compare versions and understand the evolution.
    """

    prompt = models.ForeignKey(SystemPrompt, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    content = models.TextField()
    note = models.CharField(max_length=500, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_system_prompt_version"
        constraints = [
            models.UniqueConstraint(fields=["prompt", "version"], name="unique_prompt_version_number"),
            models.CheckConstraint(check=models.Q(version__gt=0), name="prompt_version_positive"),
        ]
        ordering = ["prompt_id", "-version"]

    def __str__(self) -> str:
        return f"{self.prompt.name} v{self.version}"

    @property
    def label(self) -> str:
        return f"{self.prompt.name} v{self.version}"
