"""Services for the model registry: talking to a connection's server."""
from __future__ import annotations

import os

import httpx

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


def fetch_remote_model_ids(conn, *, timeout: float = 10) -> list[str]:
    """Model ids the connection's OpenAI-compatible ``/models`` endpoint lists, sorted.

    Raises ``ValueError`` when the connection has no base URL or the reply isn't
    a JSON model list, and ``httpx.HTTPError`` on network or HTTP failures.
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
    items = data.get("data", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    ids = {item if isinstance(item, str) else (item.get("id") or item.get("model") or "")
           for item in items if isinstance(item, (str, dict))}
    return sorted(i for i in ids if i)


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
