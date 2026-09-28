"""Judges: how a run is graded.

A Judge is a named, versioned grading method. Each JudgeVersion is immutable:
the criteria (what to evaluate) in an output format (how the grade is
reported: severity, score, yes/no or checklist), and the probe prompt the
auditor follows. A version either starts from one of SimpleAudit's judges,
keeping its format, or uses one of SimpleAudit's generic formats with its own
criteria. The model that grades is picked per run, so one judge can be run
with several models. Runs pin the exact version they used; editing a judge
saves a new version.
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
    """One immutable grading method: criteria in an output format, plus the probe prompt."""

    judge = models.ForeignKey(Judge, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    # The SimpleAudit judge this version starts from (a simpleaudit.judges
    # key; "default" = SimpleAudit's unnamed default judge). Its output format
    # comes along: fields, schema and post-processing. "" = the version uses a
    # generic format (``output`` + ``options``) with its own criteria.
    base = models.CharField(max_length=64, blank=True, default="")
    # severity | score | binary | checklist. With a base, the base's.
    output = models.CharField(max_length=16, default="severity")
    # Blank = the base's criteria. Required without a base.
    criteria = models.TextField(blank=True, default="")
    # Blank = the base's probe prompt (SimpleAudit's default without a base).
    probe_prompt = models.TextField(blank=True, default="")
    # Generic formats only: {"dimensions": [...]} for score,
    # {"question": ..., "pass_when": bool} for binary.
    options = models.JSONField(default=dict, blank=True)
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
        """The versioned fields (a new version is saved only when these change).
        Also the spec the engine builds the SimpleAudit judge from."""
        return {
            "base": self.base,
            "output": self.output,
            "criteria": self.criteria,
            "probe_prompt": self.probe_prompt,
            "options": self.options,
        }
