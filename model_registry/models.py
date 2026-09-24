"""Model registry and audit profile models.

Entities:
- ModelConnection: a provider endpoint (base_url + key + provider). First-class citizen.
- RegisteredModel: a specific model under a connection (model_id + display_name).
- ModelEndpoint: legacy flat model (deprecated, kept for backward compat with audits).
- AuditProfile: generation parameters bundle.

Credentials remain external secret references or direct (encrypted at rest in prod).
"""
from django.conf import settings
from django.db import models


class ModelConnection(models.Model):
    """A provider endpoint: one base URL + auth that serves multiple models."""
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="model_connections")
    name = models.CharField(max_length=250)
    provider = models.CharField(max_length=120, default="openai")
    base_url = models.URLField()
    secret_reference = models.CharField(max_length=250, blank=True)
    api_key_direct = models.CharField(max_length=500, blank=True, default="")
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_model_connection"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_connection_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.provider})"

    @property
    def has_key(self) -> bool:
        return bool(self.api_key_direct or self.secret_reference)


class RegisteredModel(models.Model):
    """A specific model available under a connection."""
    connection = models.ForeignKey(ModelConnection, on_delete=models.CASCADE, related_name="models")
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="registered_models")
    display_name = models.CharField(max_length=250)
    model_id = models.CharField(max_length=250)
    model_revision = models.CharField(max_length=250, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)
    default_parameters = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_registered_model"
        constraints = [
            models.UniqueConstraint(fields=["connection", "model_id"], name="unique_model_id_per_connection"),
        ]
        ordering = ["connection__name", "display_name"]

    def __str__(self) -> str:
        return f"{self.display_name} [{self.connection.name}]"

    @property
    def full_label(self) -> str:
        return f"{self.display_name} ({self.connection.name})"


class ModelEndpoint(models.Model):
    """Legacy flat model — deprecated. Use ModelConnection + RegisteredModel.
    
    Kept so existing AuditRun FKs and test fixtures continue to work.
    New UI uses ModelConnection/RegisteredModel.
    """
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="model_endpoints")
    display_name = models.CharField(max_length=250)
    provider = models.CharField(max_length=120)
    base_url = models.URLField()
    model_id = models.CharField(max_length=250)
    model_revision = models.CharField(max_length=250, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)
    default_parameters = models.JSONField(default=dict, blank=True)
    secret_reference = models.CharField(max_length=250, blank=True)
    api_key_direct = models.CharField(max_length=500, blank=True, default="")
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_model_endpoint"
        constraints = [
            models.UniqueConstraint(fields=["project", "display_name"], name="unique_model_endpoint_display_name_per_project"),
        ]
        ordering = ["project__name", "display_name"]

    def __str__(self) -> str:
        return self.display_name


class AuditProfile(models.Model):
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="audit_profiles")
    name = models.CharField(max_length=250)
    max_turns = models.PositiveIntegerField(default=4)
    temperature_target = models.FloatField(default=0.7)
    temperature_auditor = models.FloatField(default=0.2)
    temperature_judge = models.FloatField(default=0.0)
    top_p = models.FloatField(default=1.0)
    max_tokens = models.PositiveIntegerField(default=2048)
    retry_policy = models.JSONField(default=dict, blank=True)
    timeout_seconds = models.PositiveIntegerField(default=300)
    concurrency = models.PositiveIntegerField(default=1)
    language = models.CharField(max_length=30, default="en")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_audit_profile"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_audit_profile_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name
