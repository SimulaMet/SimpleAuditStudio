"""Audit run models.

AuditRun is the scientific experiment record. It must reference immutable inputs:
- pinned ScenarioSetVersion
- frozen endpoint/profile snapshots without secrets
- SimpleAudit engine version and git commit
"""
from django.conf import settings
from django.db import models


class AuditRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PREPARING = "preparing", "Preparing"
        TARGET_EXECUTION = "target_execution", "Target execution"
        AUDITING = "auditing", "Auditing"
        JUDGING = "judging", "Judging"
        AGGREGATION = "aggregation", "Aggregation"
        REPORT_GENERATION = "report_generation", "Report generation"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="audit_runs")
    name = models.CharField(max_length=250)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.QUEUED)
    scenario_set_version = models.ForeignKey("scenarios.ScenarioSetVersion", on_delete=models.RESTRICT, related_name="audit_runs")
    target_endpoint = models.ForeignKey("model_registry.ModelEndpoint", on_delete=models.RESTRICT, related_name="target_audit_runs")
    auditor_endpoint = models.ForeignKey("model_registry.ModelEndpoint", on_delete=models.RESTRICT, related_name="auditor_audit_runs")
    judge_endpoint = models.ForeignKey("model_registry.ModelEndpoint", on_delete=models.RESTRICT, related_name="judge_audit_runs")
    target_config_snapshot = models.JSONField()
    auditor_config_snapshot = models.JSONField()
    judge_config_snapshot = models.JSONField()
    generation_parameters_snapshot = models.JSONField()
    simpleaudit_version = models.CharField(max_length=120)
    git_commit = models.CharField(max_length=120)
    runtime_metadata = models.JSONField(default=dict, blank=True)
    workflow_run_id = models.CharField(max_length=250, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    total_scenarios = models.PositiveIntegerField(default=0)
    completed_scenarios = models.PositiveIntegerField(default=0)
    successful_scenarios = models.PositiveIntegerField(default=0)
    failed_scenarios = models.PositiveIntegerField(default=0)
    retried_scenarios = models.PositiveIntegerField(default=0)
    summary_metrics = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=120, blank=True)
    error_message = models.TextField(blank=True)
    # Soft-hide from the dashboard/queue. Never deletes data; the frozen
    # manifest and results stay fully accessible via the detail page.
    archived = models.BooleanField(default=False, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_audit_run"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"

    @property
    def duration_display(self) -> str:
        """Human-readable wall-clock duration, e.g. '2 minutes' or '1 hour 3 minutes'."""
        if not (self.started_at and self.finished_at):
            return ""
        seconds = int((self.finished_at - self.started_at).total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours, mins = divmod(minutes, 60)
        return f"{hours}h {mins}m"
