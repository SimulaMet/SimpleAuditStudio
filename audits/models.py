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

    # Runs in these states are finished; every other status means queued or running.
    TERMINAL_STATUSES = (Status.COMPLETED, Status.FAILED, Status.CANCELLED)

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="audit_runs")
    name = models.CharField(max_length=250)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.QUEUED)
    scenario_set_version = models.ForeignKey("scenarios.ScenarioSetVersion", on_delete=models.RESTRICT, related_name="audit_runs")
    target_model = models.ForeignKey("model_registry.RegisteredModel", on_delete=models.RESTRICT, related_name="target_audit_runs")
    auditor_model = models.ForeignKey("model_registry.RegisteredModel", on_delete=models.RESTRICT, related_name="auditor_audit_runs")
    # How the run is graded (criteria, output format, probe prompt) and the model that grades.
    judge_version = models.ForeignKey("judges.JudgeVersion", on_delete=models.RESTRICT, related_name="audit_runs")
    judge_model = models.ForeignKey("model_registry.RegisteredModel", on_delete=models.RESTRICT, related_name="judge_audit_runs")
    target_config_snapshot = models.JSONField()
    auditor_config_snapshot = models.JSONField()
    judge_config_snapshot = models.JSONField()
    generation_parameters_snapshot = models.JSONField()
    # Optional trace acquisition config (Promptfoo parity): {"mode": "builtin"|"tempo", ...}.
    # Empty/absent = no tracing (the normal black-box path). Frozen at run creation
    # like the other snapshots; the worker hands it to the engine's tracing layer.
    trace_config = models.JSONField(default=dict, blank=True)
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
    # Set when the run was launched by a recurring Monitor (drift series).
    monitor = models.ForeignKey(
        "audits.Monitor", on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
    # Set when the run was launched as one cell of an Experiment grid.
    experiment = models.ForeignKey(
        "audits.Experiment", on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_audit_run"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status not in self.TERMINAL_STATUSES

    @property
    def duration_display(self) -> str:
        """Wall-clock duration, e.g. '45s', '12m 3s' or '1h 4m' (empty until finished)."""
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


class Monitor(models.Model):
    """A recurring audit: the same frozen experiment re-run on a fixed interval.

    Each tick creates an ordinary AuditRun (linked back via ``AuditRun.monitor``)
    so every point in the drift series is a fully reproducible experiment record.
    Pin the scenario set version, auditor and judge version to keep the series
    comparable; ``scenario_set_version`` / ``judge_version`` left empty mean
    "latest version at tick".
    """

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="monitors")
    name = models.CharField(max_length=250)
    enabled = models.BooleanField(default=True)
    scenario_set = models.ForeignKey("scenarios.ScenarioSet", on_delete=models.CASCADE, related_name="monitors")
    scenario_set_version = models.ForeignKey(
        "scenarios.ScenarioSetVersion", on_delete=models.RESTRICT, null=True, blank=True, related_name="monitors"
    )
    target_model = models.ForeignKey("model_registry.RegisteredModel", on_delete=models.RESTRICT, related_name="target_monitors")
    auditor_model = models.ForeignKey("model_registry.RegisteredModel", on_delete=models.RESTRICT, related_name="auditor_monitors")
    judge_model = models.ForeignKey("model_registry.RegisteredModel", on_delete=models.RESTRICT, related_name="judge_monitors")
    judge = models.ForeignKey("judges.Judge", on_delete=models.RESTRICT, related_name="monitors")
    # Empty = the judge's newest version at each tick ("always latest").
    judge_version = models.ForeignKey(
        "judges.JudgeVersion", on_delete=models.RESTRICT, null=True, blank=True, related_name="monitors"
    )
    # Same shape as AuditRun.generation_parameters_snapshot; copied into each run.
    generation_parameters = models.JSONField(default=dict, blank=True)
    interval_hours = models.PositiveIntegerField(default=168)
    # Standard 5-field cron expression, evaluated in ``timezone``. When set it
    # replaces interval_hours as the timing rule.
    cron_expression = models.CharField(max_length=120, blank=True)
    # IANA zone the cron expression and the requested first run are read in
    # (e.g. "Europe/Oslo"); daylight-saving changes are followed. Stored
    # datetimes (next_run_at, ...) are always UTC.
    timezone = models.CharField(max_length=64, default="UTC")
    next_run_at = models.DateTimeField(db_index=True)
    last_run = models.ForeignKey("audits.AuditRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Set when the monitor repeats one run setup of an Experiment; its runs then
    # also join that experiment, so the experiment page can chart them over time.
    experiment = models.ForeignKey(
        "audits.Experiment", on_delete=models.SET_NULL, null=True, blank=True, related_name="monitors"
    )
    last_tick_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_monitor"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.interval_display})"

    @property
    def interval_display(self) -> str:
        if self.cron_expression:
            return f"cron {self.cron_expression} ({self.timezone})"
        h = self.interval_hours
        if h % 168 == 0:
            n = h // 168
            return "weekly" if n == 1 else f"every {n} weeks"
        if h % 24 == 0:
            n = h // 24
            return "daily" if n == 1 else f"every {n} days"
        return "hourly" if h == 1 else f"every {h} hours"


class Experiment(models.Model):
    """A group of runs launched together from one design.

    ``factors`` lists the inputs that vary across the runs (e.g. ["target",
    "max_turns"]); every other input is the same in all of them. See
    audits.experiments for the design, expansion and results logic.
    """

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="experiments")
    name = models.CharField(max_length=250)
    factors = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_experiment"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name
