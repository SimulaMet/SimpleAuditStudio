"""Scenario library models.

These models implement the immutable scenario versioning invariant:
- Scenario is mutable identity metadata only.
- ScenarioRevision is append-only execution content.
- ScenarioSetVersion is an immutable ordered snapshot of revisions.
- Audits must reference ScenarioSetVersion, never ScenarioSet directly.
"""
from django.conf import settings
from django.db import models


class Scenario(models.Model):
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="scenarios")
    key = models.CharField(max_length=200)
    title = models.CharField(max_length=300)
    category = models.CharField(max_length=120, blank=True)
    tags = models.JSONField(default=list, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_scenario"
        constraints = [
            models.UniqueConstraint(fields=["project", "key"], name="unique_scenario_key_per_project"),
        ]
        ordering = ["project__name", "key"]

    def __str__(self) -> str:
        return f"{self.project.slug}/{self.key}"


class ScenarioRevision(models.Model):
    scenario = models.ForeignKey(Scenario, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    description = models.TextField()
    expected_behavior = models.JSONField(default=list)
    test_prompt = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    content_hash = models.CharField(max_length=71)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_scenario_revision"
        constraints = [
            models.UniqueConstraint(fields=["scenario", "revision"], name="unique_scenario_revision_number"),
            models.CheckConstraint(check=models.Q(revision__gt=0), name="scenario_revision_positive"),
        ]
        ordering = ["scenario_id", "revision"]

    def __str__(self) -> str:
        return f"{self.scenario} rev {self.revision}"


class ScenarioSet(models.Model):
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="scenario_sets")
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_scenario_set"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_scenario_set_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name


class ScenarioSetVersion(models.Model):
    scenario_set = models.ForeignKey(ScenarioSet, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    scenario_count = models.PositiveIntegerField()
    content_hash = models.CharField(max_length=71)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_scenario_set_version"
        constraints = [
            models.UniqueConstraint(fields=["scenario_set", "version"], name="unique_set_version_number"),
            models.CheckConstraint(check=models.Q(version__gt=0), name="set_version_positive"),
        ]
        ordering = ["scenario_set_id", "version"]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        """"Set name v3": how set versions are named everywhere (see JudgeVersion.label)."""
        return f"{self.scenario_set.name} v{self.version}"


class ScenarioSetVersionItem(models.Model):
    version = models.ForeignKey(ScenarioSetVersion, on_delete=models.CASCADE, related_name="items")
    scenario = models.ForeignKey(Scenario, on_delete=models.RESTRICT, related_name="set_items")
    revision = models.ForeignKey(ScenarioRevision, on_delete=models.RESTRICT, related_name="set_items")
    position = models.PositiveIntegerField()

    class Meta:
        db_table = "core_scenario_set_version_item"
        constraints = [
            models.UniqueConstraint(fields=["version", "scenario"], name="unique_scenario_in_set_version"),
            models.UniqueConstraint(fields=["version", "position"], name="unique_position_in_set_version"),
            models.CheckConstraint(check=models.Q(position__gt=0), name="set_item_position_positive"),
        ]
        ordering = ["version_id", "position"]

    def __str__(self) -> str:
        return f"{self.version} pos {self.position}: {self.scenario}"
