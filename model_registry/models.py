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


class OtlpSpan(models.Model):
    """A span pushed to Studio's OTLP listener, persisted for cross-process reads.

    The listener (``model_registry.otlp_views.otlp_traces``) keeps an in-memory
    :class:`~simpleaudit.tracing.store.SpanStore` for the low-latency run path,
    but that memory is per-process: in production the web/API process ingests
    spans while a *separate* Hatchet worker runs the audit and needs the same
    spans to attach as evidence. The DB row is what lets the auditor read what
    the target exported -- the worker fetches by ``trace_id`` (the W3C id the
    engine propagated on the run's requests) and ``target_id``.

    Spans are append-only (idempotent on ``span_id``); retention is a separate
    sweep. ``attributes`` holds the raw OpenInference/OTel attribute map.
    """

    target_id = models.CharField(max_length=250, db_index=True)
    trace_id = models.CharField(max_length=64, db_index=True)
    span_id = models.CharField(max_length=64)
    name = models.CharField(max_length=500, default="span")
    kind = models.CharField(max_length=50, default="CHAIN")
    parent_span_id = models.CharField(max_length=64, blank=True, null=True)
    start_time = models.FloatField(null=True, blank=True)
    end_time = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=20, default="OK")
    attributes = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_otlp_span"
        constraints = [
            models.UniqueConstraint(
                fields=["target_id", "trace_id", "span_id"], name="unique_span_per_target_trace"
            ),
        ]
        ordering = ["target_id", "start_time", "span_id"]

    def __str__(self) -> str:
        return f"OtlpSpan({self.target_id}/{self.trace_id}/{self.span_id} {self.name})"


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
    # Non-secret lookup prefix of a bearer token (e.g. "sa_otlp_abc123"). Lets
    # verify_bearer do an indexed lookup instead of hashing against every
    # credential. Never used for authentication — only the salted hash is.
    token_prefix = models.CharField(max_length=32, blank=True, default="", db_index=True)
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


# ---------------------------------------------------------------------------
# Agent configuration domain
# ---------------------------------------------------------------------------


class RetrievalProfile(models.Model):
    """Query-time retrieval settings shared across agents.

    These are *query-time* knobs (top_k, rerank, threshold, …). Index-time
    settings (embedding model, chunk size) belong to the KnowledgeBase.
    """

    class SearchMode(models.TextChoices):
        SEMANTIC = "semantic", "Semantic"
        HYBRID = "hybrid", "Hybrid (semantic + BM25)"

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="retrieval_profiles")
    name = models.CharField(max_length=250)
    search_mode = models.CharField(max_length=20, choices=SearchMode.choices, default=SearchMode.SEMANTIC)
    top_k = models.PositiveIntegerField(default=5)
    rerank_enabled = models.BooleanField(default=False)
    rerank_top_k = models.PositiveIntegerField(default=4)
    relevance_threshold = models.FloatField(null=True, blank=True)
    bm25_weight = models.FloatField(null=True, blank=True)
    full_context = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_retrieval_profile"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_retrieval_profile_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name

    def config_dict(self) -> dict:
        return {
            "search_mode": self.search_mode,
            "top_k": self.top_k,
            "rerank_enabled": self.rerank_enabled,
            "rerank_top_k": self.rerank_top_k,
            "relevance_threshold": self.relevance_threshold,
            "bm25_weight": self.bm25_weight,
            "full_context": self.full_context,
        }


class KnowledgeBase(models.Model):
    """Studio-side reference to an OpenWebUI knowledge base.

    The actual documents and index live in OpenWebUI; this row carries the
    audit metadata SimpleAudit needs (authority, trust, sensitivity, version)
    and a stable ``external_id`` for lookups.
    """

    class TrustLevel(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    class Sensitivity(models.TextChoices):
        PUBLIC = "public", "Public"
        INTERNAL = "internal", "Internal"
        CONFIDENTIAL = "confidential", "Confidential"
        RESTRICTED = "restricted", "Restricted"

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="knowledge_bases")
    name = models.CharField(max_length=250)
    external_id = models.CharField(max_length=250, blank=True, default="")
    description = models.TextField(blank=True, default="")
    authority = models.CharField(max_length=250, blank=True, default="")
    trust_level = models.CharField(max_length=10, choices=TrustLevel.choices, default=TrustLevel.MEDIUM)
    sensitivity = models.CharField(max_length=20, choices=Sensitivity.choices, default=Sensitivity.INTERNAL)
    version = models.CharField(max_length=100, blank=True, default="")
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_knowledge_base"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_knowledge_base_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name


class Tool(models.Model):
    """A normalized tool that an Agent may invoke.

    The backend implementation may be an OpenWebUI built-in, an OpenAPI
    endpoint, a custom OpenWebUI function, or an MCP tool. The safety flags
    are what SimpleAudit uses to reason about what the agent *may* do.
    """

    class ToolType(models.TextChoices):
        BUILTIN = "builtin", "OpenWebUI built-in"
        OPENAPI = "openapi", "OpenAPI"
        CUSTOM = "custom", "Custom OpenWebUI function"
        MCP = "mcp", "MCP"

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="tools")
    name = models.CharField(max_length=250)
    type = models.CharField(max_length=20, choices=ToolType.choices, default=ToolType.BUILTIN)
    external_id = models.CharField(max_length=250, blank=True, default="")
    description = models.TextField(blank=True, default="")
    input_schema = models.JSONField(default=dict, blank=True)
    output_schema = models.JSONField(default=dict, blank=True)
    # Safety properties for auditing.
    read_only = models.BooleanField(default=True)
    has_side_effects = models.BooleanField(default=False)
    external_network = models.BooleanField(default=False)
    handles_sensitive_data = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_tool"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_tool_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name


class MCPServer(models.Model):
    """An MCP server that exposes tools to agents."""

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="mcp_servers")
    name = models.CharField(max_length=250)
    url = models.URLField()
    auth_config = models.JSONField(default=dict, blank=True)
    description = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_mcp_server"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_mcp_server_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name


class MCPTool(models.Model):
    """A single tool exposed by an MCP server.

    An Agent selects individual MCP tools (not the whole server) for
    least-privilege auditing.
    """

    server = models.ForeignKey(MCPServer, on_delete=models.CASCADE, related_name="tools")
    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="mcp_tools")
    external_name = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
    input_schema = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_mcp_tool"
        constraints = [
            models.UniqueConstraint(fields=["server", "external_name"], name="unique_mcp_tool_per_server"),
        ]
        ordering = ["server__name", "external_name"]

    def __str__(self) -> str:
        return f"{self.server.name}/{self.external_name}"


class Agent(models.Model):
    """An auditable RAG/agent target.

    Composes an existing ``RegisteredModel`` (the LLM), optional knowledge
    bases, tools, MCP tools, a retrieval profile, and explicit capabilities.
    The Agent is the object a user configures in Studio and then selects in
    Chat or as an audit target.
    """

    project = models.ForeignKey("accounts.Project", on_delete=models.CASCADE, related_name="agents")
    name = models.CharField(max_length=250)
    description = models.TextField(blank=True, default="")
    base_model = models.ForeignKey(RegisteredModel, on_delete=models.PROTECT, related_name="agents")
    system_prompt = models.TextField(blank=True, default="")
    knowledge_bases = models.ManyToManyField(KnowledgeBase, blank=True, related_name="agents")
    tools = models.ManyToManyField(Tool, blank=True, related_name="agents")
    mcp_tools = models.ManyToManyField(MCPTool, blank=True, related_name="agents")
    retrieval_profile = models.ForeignKey(
        RetrievalProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="agents"
    )
    # Explicit capability flags: what the agent is *permitted* to do.
    capabilities = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_agent"
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_agent_name_per_project"),
        ]
        ordering = ["project__name", "name"]

    def __str__(self) -> str:
        return self.name

    def config_snapshot(self) -> dict:
        """A serialisable snapshot of the agent's full configuration.

        Used by audit runs to freeze the configuration at execution time so
        historical runs remain reproducible.
        """
        profile = self.retrieval_profile
        return {
            "agent_id": self.id,
            "name": self.name,
            "base_model": {
                "id": self.base_model.id,
                "display_name": self.base_model.display_name,
                "model_id": self.base_model.model_id,
                "connection_id": self.base_model.connection_id,
            },
            "system_prompt": self.system_prompt,
            "knowledge_bases": list(
                self.knowledge_bases.values_list("id", "name", "external_id", "version")
            ),
            "tools": list(self.tools.values_list("id", "name", "type")),
            "mcp_tools": list(
                self.mcp_tools.values_list("id", "server__name", "external_name")
            ),
            "retrieval_profile": {
                "id": profile.id,
                "name": profile.name,
                "search_mode": profile.search_mode,
                "top_k": profile.top_k,
                "rerank_enabled": profile.rerank_enabled,
                "rerank_top_k": profile.rerank_top_k,
                "relevance_threshold": profile.relevance_threshold,
                "bm25_weight": profile.bm25_weight,
                "full_context": profile.full_context,
            } if profile else None,
            "capabilities": self.capabilities,
            "metadata": self.metadata,
        }

