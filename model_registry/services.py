"""Services for the model registry: talking to a connection's server."""
from __future__ import annotations

import os

import httpx


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
