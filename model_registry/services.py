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

    Raises ``ValueError`` when the connection has no base URL and ``httpx.HTTPError``
    on network or HTTP failures.
    """
    if not (conn.base_url or "").strip():
        raise ValueError("This connection has no base URL.")
    headers = {"Accept": "application/json"}
    key = connection_api_key(conn)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = httpx.get(models_url(conn), headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
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
    for qs in (AuditRun.objects.filter(project=project), Monitor.objects.filter(project=project)):
        for role in ("target_model", "auditor_model", "judge_model"):
            for row in qs.values(role).annotate(n=Count("id")):
                counts[row[role]] += row["n"]
    return dict(counts)
