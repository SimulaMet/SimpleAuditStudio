"""Judges: how a run is graded.

A Judge is a named, versioned grading setup. Each JudgeVersion is immutable and
bundles the judge model, the SimpleAudit rubric and the probe / judge prompts.
Runs pin the exact version they used; editing a judge saves a new version.
"""
from django.conf import settings
from django.db import models


class Judge(models.Model):
    """Identity of a judge (name and description are not versioned)."""

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="judges")
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_judge_name_per_project"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def latest(self) -> "JudgeVersion | None":
        return self.versions.order_by("-version").first()


class JudgeVersion(models.Model):
    """One immutable grading setup: model + rubric + prompts."""

    judge = models.ForeignKey(Judge, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    model = models.ForeignKey("model_registry.RegisteredModel", on_delete=models.RESTRICT, related_name="judge_versions")
    # Key of a built-in SimpleAudit judge config (simpleaudit.judges), or ""
    # for SimpleAudit's default judge. It also brings the rubric's output
    # schema and post-processing, which prompts alone can't express.
    rubric = models.CharField(max_length=64, blank=True, default="")
    # Blank = the rubric's own prompt.
    probe_prompt = models.TextField(blank=True, default="")
    judge_prompt = models.TextField(blank=True, default="")
    note = models.CharField(max_length=250, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["judge", "version"], name="unique_judge_version_number"),
            models.CheckConstraint(check=models.Q(version__gt=0), name="judge_version_positive"),
        ]
        ordering = ["judge_id", "-version"]

    def __str__(self) -> str:
        return f"{self.judge.name} v{self.version}"

    @property
    def label(self) -> str:
        return f"{self.judge.name} v{self.version}"

    def content(self) -> dict:
        """The versioned fields (a new version is saved only when these change)."""
        return {
            "model_id": self.model_id,
            "rubric": self.rubric,
            "probe_prompt": self.probe_prompt,
            "judge_prompt": self.judge_prompt,
        }
