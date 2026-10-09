"""Services for the model registry: talking to a connection's server."""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# These connections only adapt Studio agents into Open WebUI-compatible model
# references. Agents have their own page and picker section, so they are not
# user-managed provider connections.
INTERNAL_AGENT_CONNECTION_NAMES = frozenset({"Open WebUI Agent", "Open WebUI Agents"})


def is_internal_agent_connection(connection) -> bool:
    """Whether a connection is an implementation detail for a Studio agent."""
    return connection.name in INTERNAL_AGENT_CONNECTION_NAMES

# Quick-start presets for the connection form: (key, label, provider, base URL, key hint).
PROVIDER_PRESETS = [
    ("openai", "OpenAI", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    ("openrouter", "OpenRouter", "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("together", "Together AI", "together", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    ("groq", "Groq", "groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    ("ollama", "Ollama (local)", "ollama", "http://localhost:11434/v1", ""),
    ("vllm", "vLLM", "vllm", "http://localhost:8000/v1", ""),
    ("custom", "Custom (OpenAI-compatible)", "openai", "", ""),
]


def connection_api_key(conn) -> str:
    """The connection's API key: the stored key first, else the env var it names."""
    direct = (conn.api_key_direct or "").strip()
    if direct:
        return direct
    ref = (conn.secret_reference or "").strip()
    return os.environ.get(ref, "") if ref else ""


def models_url(conn) -> str:
    base = (conn.base_url or "").strip().rstrip("/")
    if conn.provider == "anthropic" and "/v1" not in base:
        return f"{base}/v1/models"
    return f"{base}/models"


def _fetch_remote_model_list(conn, *, timeout: float = 10) -> list[dict]:
    """The raw model list from the connection's ``/models`` endpoint.

    Returns ``[{"id": ..., "description": ...}, ...]`` sorted by id. Raises
    ``ValueError`` when the connection has no base URL or the reply isn't a
    JSON model list, and ``httpx.HTTPError`` on network or HTTP failures.
    """
    if not (conn.base_url or "").strip():
        raise ValueError("This connection has no base URL.")
    headers = {"Accept": "application/json"}
    key = connection_api_key(conn)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = httpx.get(models_url(conn), headers=headers, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:   # e.g. a web page: the URL isn't the API root
        raise ValueError(
            f"{models_url(conn)} didn't return a model list (the reply isn't JSON). "
            "Check the base URL: it usually ends in /v1."
        ) from exc
    items = (
        data.get("data") if isinstance(data, dict) and "data" in data
        else data.get("models") if isinstance(data, dict) and "models" in data
        else data if isinstance(data, list)
        else []
    )
    models = []
    for item in items:
        if isinstance(item, str):
            models.append({"id": item, "description": ""})
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("model") or ""
            if mid:
                models.append({"id": mid, "description": item.get("description") or ""})
    return sorted(models, key=lambda m: m["id"])


def fetch_remote_model_ids(conn, *, timeout: float = 10) -> list[str]:
    """Model ids the connection's OpenAI-compatible ``/models`` endpoint lists, sorted."""
    return [m["id"] for m in _fetch_remote_model_list(conn, timeout=timeout)]


# ─── Decision-model detection ────────────────────────────────────────────────

#: Model ids in Ollama's decision-model catalog (https://ollama.com/search?c=decision),
#: matched by model name so that ``library/clef`` and ``clef`` both count.
OLLAMA_LIBRARY_DECISION_MODELS = frozenset({"clef", "clef-flash", "laya", "nimble", "tev1"})

#: The probe question for a decision-model check: two options, the minimum
#: the System One endpoint accepts.
_DECISION_PROBE_BODY = {
    "state": "capability probe",
    "questions": {
        "probe": {"type": "choice", "instructions": "Pick one.", "criteria": {"yes": "Yes", "no": "No"}}
    },
}


def probe_systemone_decision(base_url: str, model_id: str, *, timeout: float = 15) -> bool | None:
    """Whether a model answers on the server's ``POST /v1/systemone`` (True/False).

    The System One endpoint is a shared surface: Ollama (>= 0.35) and vLLM
    both expose it at the API root, and every OpenAI-compatible server can
    be probed the same way. ``None`` when it cannot be told: network errors,
    a server not exposing the endpoint (404), or an unrecognised reply.
    """
    base = (base_url or "").strip().rstrip("/")
    url = f"{base.removesuffix('/v1')}/v1/systemone"
    body = {**_DECISION_PROBE_BODY, "model": model_id}
    try:
        resp = httpx.post(url, json=body, timeout=timeout)
    except httpx.HTTPError:
        return None
    if resp.status_code == 200:
        return True
    # 400 with Ollama's explicit message and 501 (vLLM's "no supported read
    # strategy") are definitive "not a decision model" answers.
    if resp.status_code == 501:
        return False
    if resp.status_code == 400 and "does not support decision" in (resp.text or ""):
        return False
    return None


def _is_openrouter_decision_model(model_id: str) -> bool:
    """Heuristic: OpenRouter's decision (System One) models, e.g. ``typesafe/jev-1.13``."""
    return "typesafe/" in model_id or "jev" in model_id


def detect_model_capabilities(conn, model_id: str, *, timeout: float = 10) -> dict:
    """Best-effort capability flags for one model on a connection.

    A model that cannot be classified simply carries no ``decision`` flag:
    - Ollama and OpenAI-compatible servers (vLLM, …): one
      ``POST /v1/systemone`` probe (a decision model answers the real
      two-option question in milliseconds; anything else gets a fast 400/501
      with no inference). Unknown Ollama library decision models are
      recognised by name as a fallback.
    - OpenRouter: a name match (``typesafe/…`` / ``jev…``); its
      ``/v1/systemone`` endpoint is a paid proxy with no public model
      listing, so probing would bill real inference.
    """
    caps: dict = {}
    if conn.provider == "openrouter":
        if _is_openrouter_decision_model(model_id):
            caps["decision"] = True
    else:
        supports = probe_systemone_decision(conn.base_url, model_id, timeout=timeout)
        if supports is not None:
            caps["decision"] = bool(supports)
        elif conn.provider == "ollama" and model_id.rsplit(":", 1)[0].rsplit("/", 1)[-1] in OLLAMA_LIBRARY_DECISION_MODELS:
            caps["decision"] = True
    return caps


def fetch_remote_models(conn, *, timeout: float = 10) -> list[dict]:
    """Models the connection offers, each with a ``capabilities`` dict (see detect_model_capabilities)."""
    return [
        {"id": m["id"], "description": m["description"], "capabilities": detect_model_capabilities(conn, m["id"], timeout=timeout)}
        for m in _fetch_remote_model_list(conn, timeout=timeout)
    ]


def http_error_detail(exc: Exception) -> str:
    """Short, user-facing description of a failed call to a model server."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {exc.response.text[:200] or exc.response.reason_phrase}"
    return str(exc) or type(exc).__name__


def model_usage_counts(project) -> dict[int, int]:
    """Runs and monitors that reference each model (as target, auditor or judge)."""
    from collections import Counter

    from django.db.models import Count

    from audits.models import AuditRun, Monitor

    counts: Counter = Counter()
    for qs, roles in (
        (AuditRun.objects.filter(project=project), ("target_model", "auditor_model", "judge_model")),
        (Monitor.objects.filter(project=project), ("target_model", "auditor_model", "judge_model")),
    ):
        for role in roles:
            for row in qs.values(role).annotate(n=Count("id")):
                counts[row[role]] += row["n"]
    return dict(counts)


# ─── Connection sharing / visibility ─────────────────────────────────────────


def admin_workspaces(user) -> list:
    """Workspaces where ``user`` holds the ADMIN role (superusers get all).

    Used to resolve the "admins" visibility level and to populate the
    shared-with picker. Returns a list of Project objects ordered by name.
    """
    from accounts.models import Project, ProjectMembership

    if not user or not user.is_authenticated:
        return []
    if user.is_superuser:
        return list(Project.objects.order_by("name"))
    ids = list(
        ProjectMembership.objects.filter(
            user=user, role=ProjectMembership.Role.ADMIN
        ).values_list("project_id", flat=True)
    )
    return list(Project.objects.filter(pk__in=ids).order_by("name"))


def visible_connections_for(user, project):
    """ModelConnections the user may see/use while working in ``project``.

    A connection is visible when any of these hold:
      * it belongs to ``project`` (the owner workspace — always visible), or
      * its visibility is PUBLIC, or
      * its visibility is ADMINS and the user is an admin of ``project``, or
      * ``project`` is explicitly listed in its ``shared_with``.

    The owning workspace's connections come first, then shared ones, each
    group ordered by name. Annotates each with ``is_owner`` (True when the
    connection's project is ``project``) so templates can gate editing.
    """
    from django.db.models import Q

    from .models import ModelConnection

    if not user or not user.is_authenticated or project is None:
        return ModelConnection.objects.none()

    base = ModelConnection.objects.all().select_related("project")
    owner_qs = base.filter(project=project)
    shared_filter = Q(visibility=ModelConnection.Visibility.PUBLIC) | Q(shared_with=project)
    if _user_is_admin_of(user, project):
        shared_filter |= Q(visibility=ModelConnection.Visibility.ADMINS)
    shared_qs = base.exclude(project=project).filter(shared_filter)
    result = list(owner_qs.order_by("name")) + list(shared_qs.order_by("name"))
    for conn in result:
        conn.is_owner = conn.project_id == project.id
    return result


def _user_is_admin_of(user, project) -> bool:
    """True when ``user`` is a superuser or ADMIN member of ``project``."""
    from accounts.models import ProjectMembership

    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return ProjectMembership.objects.filter(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    ).exists()


def visible_connection_ids_for(project) -> list[int]:
    """Ids of connections usable in ``project`` (owner + shared into it).

    Used by run-creation validation to accept models whose connection is
    shared into the workspace. The "admins" level is resolved against the
    *workspace's* admins (any admin of ``project`` can use an admin-shared
    connection), so no specific user is needed here.
    """
    from django.db.models import Q

    from .models import ModelConnection

    ids = set(ModelConnection.objects.filter(project=project).values_list("id", flat=True))
    ids |= set(
        ModelConnection.objects.exclude(project=project)
        .filter(
            Q(visibility=ModelConnection.Visibility.PUBLIC)
            | Q(shared_with=project)
            | Q(visibility=ModelConnection.Visibility.ADMINS)
        )
        .values_list("id", flat=True)
    )
    return list(ids)


def can_edit_connection(user, conn) -> bool:
    """Whether ``user`` may edit/delete this connection.

    Only the owning workspace's admins (or a superuser) may change a
    connection. Other workspaces that can *see* a shared connection are
    read-only: they see the description but cannot edit it.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    from accounts.models import ProjectMembership

    return ProjectMembership.objects.filter(
        project=conn.project, user=user, role=ProjectMembership.Role.ADMIN
    ).exists()


def connection_share_label(conn) -> str:
    """Short human label describing a connection's sharing level."""
    from .models import ModelConnection

    if conn.visibility == ModelConnection.Visibility.PUBLIC:
        return "Public — every workspace"
    if conn.visibility == ModelConnection.Visibility.ADMINS:
        return "Shared with my admin workspaces"
    return "This workspace only"


# --- Open WebUI sync orchestration ------------------------------------------
#
# Open WebUI is the source of truth for agent/KB/tool *content and behavior*;
# Studio keeps the durable reference rows (identity + audit metadata) and
# pushes on create/edit. Every function below is a no-op returning
# ``"skipped"`` when chat is disabled, and never raises — a flaky Open WebUI
# must not break a Studio request.


def _chat_sync_enabled() -> bool:
    from chat import config as chat_config

    return bool(chat_config.ENABLED)


def _adapter_for(user):
    from chat.api import ChatAPI
    from integrations.openwebui.client import OpenWebUIAdapter

    return OpenWebUIAdapter(ChatAPI.as_user(user))


def sync_agent_to_openwebui(agent, user) -> str:
    """Push an agent to Open WebUI (create-or-update its model entry).

    Returns ``"created"``, ``"updated"`` or ``"skipped"`` (chat disabled or
    sync failed). Failures are logged and left for the next edit to retry.
    """
    if not _chat_sync_enabled():
        return "skipped"
    try:
        adapter = _adapter_for(user)
        result = adapter.push_agent(agent)
        _ensure_agent_model_reference(agent, user)
        return result["status"]
    except Exception:
        logger.exception("Agent '%s' Open WebUI sync failed", agent.name)
        return "skipped"


def _ensure_agent_model_reference(agent, user) -> None:
    """Register the synced OpenWebUI agent as one Studio picker model.

    Agents are workspace models in OpenWebUI, but the audit design form works
    with ``RegisteredModel`` rows. Keep one internal OpenWebUI-compatible
    connection/model reference so the same agent can be selected as target,
    auditor, or judge without exposing a second representation.
    """
    if not agent.external_id:
        return
    from chat import config as chat_config
    from model_registry.models import ModelConnection, RegisteredModel

    model = (
        RegisteredModel.objects.select_related("connection")
        .filter(project=agent.project, model_id=agent.external_id)
        .first()
    )
    if model is not None:
        if model.display_name != agent.name or not model.enabled:
            model.display_name = agent.name
            model.description = agent.description or "Synced Studio agent in OpenWebUI."
            model.enabled = agent.enabled
            model.save(update_fields=["display_name", "description", "enabled", "updated_at"])
        return

    connection, _ = ModelConnection.objects.get_or_create(
        project=agent.project,
        name="Open WebUI Agents",
        defaults={
            "provider": "openai",
            "base_url": f"{chat_config.UPSTREAM}/api/v1",
            "description": "Managed references for Studio agents synced to OpenWebUI.",
            "enabled": True,
            "created_by": user,
        },
    )
    RegisteredModel.objects.create(
        connection=connection,
        project=agent.project,
        model_id=agent.external_id,
        display_name=agent.name,
        description=agent.description or "Synced Studio agent in OpenWebUI.",
        enabled=agent.enabled,
        created_by=user,
    )


def agent_target_model(agent):
    """Return the single RegisteredModel representing an Open WebUI Agent."""
    if not agent.external_id:
        raise ValueError("Agent is not synced to Open WebUI and has no external_id.")
    from model_registry.models import RegisteredModel

    model = (RegisteredModel.objects.select_related("connection")
             .filter(project=agent.project, model_id=agent.external_id,
                     connection__name="Open WebUI Agents").first())
    if model is None:
        _ensure_agent_model_reference(agent, agent.created_by)
        model = (RegisteredModel.objects.select_related("connection")
                 .filter(project=agent.project, model_id=agent.external_id,
                         connection__name="Open WebUI Agents").first())
    if model is None:
        raise ValueError("Agent is not synced to the Open WebUI Agents model registry.")
    return model


def agent_live_openwebui(agent, user) -> dict | None:
    """The live Open WebUI model entry for an agent, for detail-page display.

    ``None`` means "no live view" (chat disabled, agent never synced, entry
    deleted in Open WebUI, or Open WebUI down) — callers fall back to the
    local cached row. Never raises.
    """
    if not _chat_sync_enabled() or not agent.external_id:
        return None
    try:
        return _adapter_for(user).agent_remote(agent)
    except Exception:
        logger.exception("Agent '%s' Open WebUI live fetch failed", agent.name)
        return None


def delete_agent_from_openwebui(agent) -> None:
    """Delete an agent's Open WebUI model entry (best-effort, never raises)."""
    if not _chat_sync_enabled() or not agent.external_id:
        return
    try:
        _adapter_for(_any_user()).delete_agent(agent)
    except Exception:
        logger.exception("Agent '%s' Open WebUI delete failed", agent.name)


def sync_knowledge_base_to_openwebui(kb, user) -> str:
    """Create or update a knowledge base in Open WebUI.

    New KBs (no ``external_id``) get a fresh entry; existing ones are renamed /
    re-described in place. Returns the new status or ``"skipped"``.
    """
    if not _chat_sync_enabled():
        return "skipped"
    try:
        adapter = _adapter_for(user)
        if kb.external_id:
            adapter._api.update_knowledge_base(kb.external_id, kb.name, kb.description)
            return "updated"
        row = adapter._api.create_knowledge_base(kb.name, kb.description)
        kb.external_id = row.get("id", "")
        kb.save(update_fields=["external_id", "updated_at"])
        return "created"
    except Exception:
        logger.exception("Knowledge base '%s' Open WebUI sync failed", kb.name)
        return "skipped"


def delete_knowledge_base_from_openwebui(kb) -> None:
    if not _chat_sync_enabled() or not kb.external_id:
        return
    try:
        _adapter_for(_any_user())._api.delete_knowledge_base(kb.external_id)
    except Exception:
        logger.exception("Knowledge base '%s' Open WebUI delete failed", kb.name)


def sync_tool_to_openwebui(tool, user, *, content: str) -> str:
    """Create or update a tool in Open WebUI from toolkit-module ``content``.

    Studio does not persist tool source (Open WebUI owns it), so the caller
    supplies the content — e.g. the uploaded file body on create, or the
    untouched remote content on a metadata-only edit.
    """
    if not _chat_sync_enabled():
        return "skipped"
    try:
        adapter = _adapter_for(user)
        tool_id = f"studio.tool-{tool.pk}"
        if tool.external_id:
            adapter._api.update_tool(tool.external_id, tool.name, content, tool.description)
            return "updated"
        row = adapter._api.create_tool(tool_id, tool.name, content, tool.description)
        tool.external_id = row.get("id", "") or tool_id
        tool.save(update_fields=["external_id", "updated_at"])
        return "created"
    except Exception:
        logger.exception("Tool '%s' Open WebUI sync failed", tool.name)
        return "skipped"


def delete_tool_from_openwebui(tool) -> None:
    if not _chat_sync_enabled() or not tool.external_id:
        return
    try:
        _adapter_for(_any_user())._api.delete_tool(tool.external_id)
    except Exception:
        logger.exception("Tool '%s' Open WebUI delete failed", tool.name)


def _any_user():
    """An authenticated user for a deletion-time API call (admin is cleanest)."""
    from django.contrib.auth import get_user_model

    return (
        get_user_model().objects.filter(is_superuser=True).order_by("id").first()
        or get_user_model().objects.order_by("id").first()
    )
