"""Model registry models.

Entities:
- ModelConnection: a provider endpoint (base_url + key + provider). First-class citizen.
- RegisteredModel: a specific model under a connection (model_id + display_name).
Credentials remain external secret references or direct (encrypted at rest in prod).
"""
from django.conf import settings
from django.db import models


class ModelConnection(models.Model):
    """A provider endpoint: one base URL + auth that serves multiple models.

    Sharing (``visibility`` + ``shared_with``) controls which workspaces can
    *see* and *use* this connection's models. The owner workspace always sees
    and edits it; other workspaces see it read-only (description visible, no
    edit). The API key stays with the owner — consumers use the owner's key.
    """

    class Visibility(models.TextChoices):
        WORKSPACE = "workspace", "This workspace only"
        ADMINS = "admins", "Workspaces where I'm admin"
        PUBLIC = "public", "Every workspace (public)"

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="model_connections")
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
    provider = models.CharField(max_length=120, default="openai")
    base_url = models.URLField()
    secret_reference = models.CharField(max_length=250, blank=True)
    api_key_direct = models.CharField(max_length=500, blank=True, default="")
    enabled = models.BooleanField(default=True)
    # Who may see/use this connection outside the owning workspace.
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.WORKSPACE
    )
    # Explicitly shared workspaces (used when visibility == ADMINS is too broad
    # or to narrow a public share). A workspace in this list can see the
    # connection even if its visibility would otherwise hide it.
    shared_with = models.ManyToManyField(
        "accounts.Project",
        blank=True,
        related_name="shared_connections",
    )
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

    @property
    def is_public(self) -> bool:
        return self.visibility == self.Visibility.PUBLIC

    @property
    def is_shared_to_admins(self) -> bool:
        return self.visibility == self.Visibility.ADMINS


class RegisteredModel(models.Model):
    """A specific model available under a connection."""
    connection = models.ForeignKey(ModelConnection, on_delete=models.CASCADE, related_name="models")
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="registered_models")
    display_name = models.CharField(max_length=250)
    model_id = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
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
    def has_key(self) -> bool:
        """Whether the parent connection has an API key configured."""
        return self.connection.has_key


class OTLPCredential(models.Model):
    """A credential that lets an external target push OTLP traces to Studio.

    One credential per target. The target is configured with either a Basic
    username+password (standard ``OTEL_BASIC_AUTH_*``) or a Bearer
    token (generic OTel exporters). The receiver authenticates the request and
    tags every ingested span with ``target_id`` so spans are attributable.

    Only a salted hash of the secret is stored — the plaintext password/token
    is shown once at creation and cannot be read back.
    """

    class AuthMode(models.TextChoices):
        NONE = "none", "None (open)"
        BASIC = "basic", "Basic Auth"
        BEARER = "bearer", "Bearer Token"

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="otlp_credentials")
    connection = models.ForeignKey(ModelConnection, on_delete=models.CASCADE, related_name="otlp_credentials")
    auth_mode = models.CharField(max_length=10, choices=AuthMode.choices, default=AuthMode.BASIC)
    # For basic: the username the target sends. For bearer: a stable label.
    username = models.CharField(max_length=250, blank=True, default="")
    # Salted SHA-256 of the secret (password for basic, token for bearer).
    secret_hash = models.BinaryField(null=True, blank=True)
    salt = models.BinaryField(null=True, blank=True)
    # Stable identifier stamped onto every span this credential authenticates.
    target_id = models.CharField(max_length=250)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_otlp_credential"
        constraints = [
            models.UniqueConstraint(fields=["project", "target_id"], name="unique_target_id_per_project"),
        ]
        ordering = ["project__name", "target_id"]

    def __str__(self) -> str:
        return f"OTLP {self.auth_mode}:{self.target_id}"

    @property
    def display_name(self) -> str:
        return self.username or self.target_id

