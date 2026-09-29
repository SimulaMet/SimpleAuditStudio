"""Talking to Open WebUI's API from Studio — both directions.

Studio already sits on the trusted side of the chat proxy, so it authenticates
the same way the proxy makes the browser authenticate: POST the trusted identity
headers to ``/api/v1/auths/signin`` and use the token that comes back. No API key
to provision, no second set of credentials, and the call runs as a real Open
WebUI user with that user's role.

    Studio ──(X-Studio-* headers)──► /api/v1/auths/signin ──► token
           ──(Bearer token)────────► the rest of the API

Push (Studio → Open WebUI): a Studio model connection is a base URL plus a key,
which is exactly Open WebUI's OpenAI-compatible provider config, so connections
map onto ``OPENAI_API_BASE_URLS``/``OPENAI_API_KEYS``/``OPENAI_API_CONFIGS``.
Open WebUI keeps those as parallel lists that anyone can also edit by hand, so
each pushed entry carries a marker (``simpleaudit_connection_id``) in its config.
A sync replaces the marked entries and leaves everything else alone.

Pull (Open WebUI → Studio): knowledge bases are read through ``/api/v1/knowledge``
and normalised to plain dicts, so callers never see Open WebUI's schema.

This module never talks to the proxy — it addresses Open WebUI directly on
``chat.UPSTREAM``, which is reachable from the Studio process only.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from chat import config as chat

logger = logging.getLogger(__name__)

#: Marks a provider entry as one Studio owns, so a sync can replace exactly
#: those and leave hand-added ones untouched.
STUDIO_MARKER = "simpleaudit_connection_id"

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ChatAPIError(RuntimeError):
    """Open WebUI refused or could not be reached."""


class ChatAPI:
    """An Open WebUI session for one Studio user.

    ``ChatAPI.as_user(user)`` is the normal entry point. Pushing provider config
    needs an Open WebUI admin, which means a Studio superuser or a workspace
    admin (see ``chat.config.identity``).
    """

    def __init__(self, identity: dict[str, str], *, base_url: str | None = None):
        self.identity = identity
        self.base_url = (base_url or chat.UPSTREAM).rstrip("/")
        self._token: str | None = None

    @classmethod
    def as_user(cls, user) -> ChatAPI:
        return cls(chat.identity(user))

    # --- plumbing ----------------------------------------------------------
    def sign_in(self) -> dict[str, Any]:
        """Exchange the trusted headers for a token. Creates the account on first use."""
        response = self._send(
            "POST", "/api/v1/auths/signin",
            json={"email": "", "password": ""},   # the headers carry the identity
            headers=self.identity,
        )
        self._token = response.get("token")
        if not self._token:
            raise ChatAPIError("Open WebUI signed us in but returned no token.")
        return response

    def request(self, method: str, path: str, json: Any | None = None) -> Any:
        if self._token is None:
            self.sign_in()
        return self._send(method, path, json=json,
                          headers={"Authorization": f"Bearer {self._token}"})

    def _send(self, method: str, path: str, *, json: Any | None, headers: dict[str, str]) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = httpx.request(method, url, json=json, headers=headers, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ChatAPIError(f"Could not reach Open WebUI at {url}: {exc}") from exc
        if response.status_code >= 400:
            raise ChatAPIError(f"{method} {path} failed ({response.status_code}): {response.text[:300]}")
        return response.json() if response.content else None

    # --- push: Studio connections -> Open WebUI providers -------------------
    def openai_config(self) -> dict[str, Any]:
        return self.request("GET", "/openai/config")

    def set_openai_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/openai/config/update", json=config)

    def push_connections(self, connections: list[dict[str, Any]]) -> dict[str, int]:
        """Make Open WebUI's provider list match these Studio connections.

        Returns how many entries were pushed and how many foreign ones survived.
        """
        current = self.openai_config()
        planned = plan_openai_config(current, connections)
        self.set_openai_config(planned)
        return {
            "pushed": len(connections),
            "kept": len(planned["OPENAI_API_BASE_URLS"]) - len(connections),
        }

    # --- pull: Open WebUI knowledge -> Studio -------------------------------
    def knowledge_bases(self) -> list[dict[str, Any]]:
        """Every knowledge base this user can read, as plain dicts."""
        payload = self.request("GET", "/api/v1/knowledge/")
        return [_knowledge_summary(item) for item in _as_list(payload)]

    def knowledge_base(self, knowledge_id: str) -> dict[str, Any]:
        """One knowledge base, with the names of the files in it."""
        item = self.request("GET", f"/api/v1/knowledge/{knowledge_id}")
        summary = _knowledge_summary(item)
        summary["files"] = [
            {
                "id": file.get("id"),
                "name": (file.get("meta") or {}).get("name") or file.get("filename") or "",
            }
            for file in (item.get("files") or [])
        ]
        return summary


# --- pure helpers (no I/O, so they are cheap to test) ----------------------
def connection_payload(conn) -> dict[str, Any]:
    """The part of a Studio ModelConnection that Open WebUI needs."""
    from model_registry.services import connection_api_key

    return {
        "id": conn.id,
        "name": conn.name,
        "base_url": (conn.base_url or "").strip().rstrip("/"),
        "api_key": connection_api_key(conn),
        "enabled": conn.enabled,
    }


def plan_openai_config(current: dict[str, Any], connections: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge Studio's connections into Open WebUI's OpenAI provider config.

    Entries Studio pushed before (they carry ``STUDIO_MARKER``) are replaced;
    entries somebody added in Open WebUI itself are kept, in their order, with
    their config re-keyed to their new index.
    """
    urls = list(current.get("OPENAI_API_BASE_URLS") or [])
    keys = list(current.get("OPENAI_API_KEYS") or [])
    configs = dict(current.get("OPENAI_API_CONFIGS") or {})
    keys += [""] * (len(urls) - len(keys))

    kept = [
        (url, keys[index], configs.get(str(index), {}))
        for index, url in enumerate(urls)
        if STUDIO_MARKER not in configs.get(str(index), {})
    ]
    ours = [
        (
            connection["base_url"],
            connection.get("api_key", ""),
            {
                STUDIO_MARKER: connection["id"],
                "enable": bool(connection.get("enabled", True)),
                # Shown in Open WebUI's admin UI, so it reads as the Studio name.
                "name": connection["name"],
            },
        )
        for connection in connections
    ]

    merged = kept + ours
    return {
        "ENABLE_OPENAI_API": True,
        "OPENAI_API_BASE_URLS": [url for url, _, _ in merged],
        "OPENAI_API_KEYS": [key for _, key, _ in merged],
        "OPENAI_API_CONFIGS": {str(index): config for index, (_, _, config) in enumerate(merged)},
    }


def _as_list(payload: Any) -> list[dict[str, Any]]:
    """Open WebUI returns either a bare list or {"items": [...]} depending on route."""
    if isinstance(payload, dict):
        for field in ("items", "knowledge_bases", "data"):
            if isinstance(payload.get(field), list):
                return payload[field]
        return []
    return payload or []


def _knowledge_summary(item: dict[str, Any]) -> dict[str, Any]:
    files = item.get("files")
    return {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "description": item.get("description") or "",
        "file_count": len(files) if isinstance(files, list) else item.get("file_count"),
        "updated_at": item.get("updated_at"),
    }
