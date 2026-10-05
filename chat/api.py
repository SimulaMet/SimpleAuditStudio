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

import json as jsonlib
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
        if not response.content:
            return None
        try:
            return response.json()
        except (jsonlib.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ChatAPIError(
                f"{method} {path} returned non-JSON content "
                f"({response.status_code}, {response.headers.get('content-type', 'unknown')})"
            ) from exc

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

    def disable_ollama(self) -> None:
        """Turn Ollama off in Open WebUI's settings.

        Nothing in a Studio deployment serves Ollama, but Open WebUI polls it on
        every page load — a 500 in the browser console each time — and shows an
        empty Ollama section in the connection settings. The environment variable
        only seeds the first start, so an instance that already has it on has to
        be told.
        """
        current = self.request("GET", "/ollama/config")
        if current.get("ENABLE_OLLAMA_API") is False:
            return
        self.request("POST", "/ollama/config/update", json={
            "ENABLE_OLLAMA_API": False,
            "OLLAMA_BASE_URLS": current.get("OLLAMA_BASE_URLS") or [],
            "OLLAMA_API_CONFIGS": current.get("OLLAMA_API_CONFIGS") or {},
        })

    # --- groups / access grants --------------------------------------------
    def create_group(self, name: str, description: str, *, data: dict | None = None, meta: dict | None = None) -> dict[str, Any]:
        return self.request("POST", "/api/v1/groups/create", json={
            "name": name, "description": description, "data": data or {}, "meta": meta or {},
        })

    def list_groups(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/api/v1/groups/")
        return payload if isinstance(payload, list) else []

    def get_group(self, group_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/groups/id/{group_id}")

    def update_group(self, group_id: str, name: str, description: str, *, data: dict | None = None, meta: dict | None = None) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/groups/id/{group_id}/update", json={
            "name": name, "description": description, "data": data or {}, "meta": meta or {},
        })

    def group_user_ids(self, group_id: str) -> list[str]:
        payload = self.request("GET", f"/api/v1/groups/id/{group_id}/export")
        return list(payload.get("user_ids") or []) if isinstance(payload, dict) else []

    def add_group_users(self, group_id: str, user_ids: list[str]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/groups/id/{group_id}/users/add", json={"user_ids": user_ids})

    def remove_group_users(self, group_id: str, user_ids: list[str]) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/groups/id/{group_id}/users/remove", json={"user_ids": user_ids})

    def update_model_access(self, model_id: str, access_grants: list[dict[str, str]], *, name: str | None = None) -> Any:
        return self.request("POST", "/api/v1/models/model/access/update", json={
            "id": model_id, "name": name or model_id, "access_grants": access_grants,
        })

    def update_current_user_settings(self, settings: dict[str, Any]) -> Any:
        """Patch the signed-in user's Open WebUI settings."""
        return self.request("POST", "/api/v1/users/user/settings/update", json=settings)

    # --- admin: server-side retrieval configuration ------------------------
    def retrieval_config(self) -> dict[str, Any]:
        """Read Open WebUI's server-side RAG/document configuration.

        Open WebUI enforces admin access on this endpoint. Callers must use a
        ChatAPI session for an Open WebUI administrator.
        """
        payload = self.request("GET", "/api/v1/retrieval/config")
        return payload if isinstance(payload, dict) else {}

    def embedding_config(self) -> dict[str, Any]:
        """Read the server-side embedding configuration (admin-only)."""
        payload = self.request("GET", "/api/v1/retrieval/embedding")
        return payload if isinstance(payload, dict) else {}

    def update_retrieval_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Patch server-side RAG/document settings (admin-only)."""
        payload = self.request("POST", "/api/v1/retrieval/config/update", json=config)
        return payload if isinstance(payload, dict) else {}

    def update_embedding_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Update server-side embedding settings (admin-only)."""
        payload = self.request("POST", "/api/v1/retrieval/embedding/update", json=config)
        return payload if isinstance(payload, dict) else {}

    def subagents_config(self) -> dict[str, Any]:
        """Read Open WebUI's admin sub-agent configuration."""
        payload = self.request("GET", "/api/v1/configs/subagents")
        return payload if isinstance(payload, dict) else {}

    def update_subagents_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Update Open WebUI's admin sub-agent configuration."""
        payload = self.request("POST", "/api/v1/configs/subagents", json=config)
        return payload if isinstance(payload, dict) else {}

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

    # --- push: resources -> Open WebUI (one-time seeding / admin) ----------
    def create_knowledge_base(self, name: str, description: str = "") -> dict[str, Any]:
        """Create a knowledge base in Open WebUI. Returns the created row (has ``id``)."""
        return self.request(
            "POST", "/api/v1/knowledge/create",
            json={"name": name, "description": description},
        )

    def add_file_to_knowledge_base(self, knowledge_id: str, file_id: str) -> Any:
        """Link an already-uploaded file (see ``upload_file``) to a knowledge base."""
        return self.request(
            "POST", f"/api/v1/knowledge/{knowledge_id}/file/add",
            json={"file_id": file_id},
        )

    def create_tool(self, tool_id: str, name: str, content: str, description: str = "") -> dict[str, Any]:
        """Register a Python tool in Open WebUI's toolkit (the Tools page).

        ``content`` must be a complete toolkit module: a module docstring with
        ``name:``/``description:``/``category:`` frontmatter and a ``Tools``
        class whose public methods are the tools (type hints + docstrings).
        Open WebUI executes the code server-side and introspects the class to
        build the spec, so invalid content is rejected with a 400.
        """
        return self.request(
            "POST", "/api/v1/tools/create",
            json={
                "id": tool_id,
                "name": name,
                "content": content,
                "meta": {"description": description, "manifest": {}},
            },
        )

    def tools(self) -> list[dict[str, Any]]:
        """Every tool registered in Open WebUI's toolkit, as plain dicts."""
        payload = self.request("GET", "/api/v1/tools/")
        return _as_list(payload)

    def upload_file(self, filename: str, content: bytes, content_type: str = "text/markdown") -> dict[str, Any]:
        """Upload a raw file to Open WebUI's file store. Returns the file row (has ``id``).

        Uses multipart/form-data, which the JSON-only ``request`` helper cannot do.
        """
        if self._token is None:
            self.sign_in()
        url = f"{self.base_url}/api/v1/files/"
        files = {"file": (filename, content, content_type)}
        try:
            response = httpx.post(
                url, files=files,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ChatAPIError(f"Could not reach Open WebUI at {url}: {exc}") from exc
        if response.status_code >= 400:
            raise ChatAPIError(f"POST /api/v1/files/ failed ({response.status_code}): {response.text[:300]}")
        try:
            return response.json()
        except (jsonlib.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ChatAPIError(
                f"POST /api/v1/files/ returned non-JSON content ({response.status_code})."
            ) from exc

    # --- agents as Open WebUI "workspace models" ---------------------------
    def create_workspace_model(
        self, model_id: str, name: str, *, base_model_id: str | None = None,
        description: str = "", knowledge: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Studio agent as an Open WebUI workspace model entry.

        The entry inherits ``base_model_id`` (the pushed connection's model)
        and attaches knowledge bases via ``meta.knowledge`` (file-shaped
        entries the chat runtime reads at completion time). Returns the
        created row. Raises ``ChatAPIError`` when the id is taken.
        """
        meta: dict[str, Any] = {"description": description or None}
        if knowledge is not None:
            meta["knowledge"] = knowledge
        if metadata:
            meta.update(metadata)
        return self.request(
            "POST", "/api/v1/models/create",
            json={
                "id": model_id,
                "name": name,
                "base_model_id": base_model_id,
                "meta": meta,
                "params": params or {},
            },
        )

    def update_workspace_model(
        self, model_id: str, name: str, *, base_model_id: str | None = None,
        description: str = "", knowledge: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an existing workspace model entry (same shape as create)."""
        meta: dict[str, Any] = {"description": description or None}
        if knowledge is not None:
            meta["knowledge"] = knowledge
        if metadata:
            meta.update(metadata)
        return self.request(
            "POST", "/api/v1/models/model/update",
            json={
                "id": model_id,
                "name": name,
                "base_model_id": base_model_id,
                "meta": meta,
                "params": params or {},
            },
        )

    def get_workspace_model(self, model_id: str) -> dict[str, Any]:
        """One workspace model entry. The id may contain '/', so it goes in the query."""
        return self.request("GET", f"/api/v1/models/model?id={model_id}")

    def delete_workspace_model(self, model_id: str) -> bool:
        return self.request("POST", "/api/v1/models/model/delete", json={"id": model_id})

    # --- KB / tool writes (sync on create/edit) ----------------------------
    def update_knowledge_base(self, knowledge_id: str, name: str, description: str) -> dict[str, Any]:
        """Rename / re-describe an existing Open WebUI knowledge base."""
        return self.request(
            "POST", f"/api/v1/knowledge/{knowledge_id}/update",
            json={"name": name, "description": description},
        )

    def delete_knowledge_base(self, knowledge_id: str) -> bool:
        return self.request("DELETE", f"/api/v1/knowledge/{knowledge_id}/delete")

    def get_tool(self, tool_id: str) -> dict[str, Any]:
        """One toolkit tool, with content and specs."""
        return self.request("GET", f"/api/v1/tools/id/{tool_id}")

    def update_tool(self, tool_id: str, name: str, content: str, description: str = "") -> dict[str, Any]:
        """Replace a toolkit tool's source / metadata. ``content`` must stay
        a valid toolkit module (same format as ``create_tool``)."""
        return self.request(
            "POST", f"/api/v1/tools/id/{tool_id}/update",
            json={
                "id": tool_id,
                "name": name,
                "content": content,
                "meta": {"description": description, "manifest": {}},
            },
        )

    def delete_tool(self, tool_id: str) -> bool:
        return self.request("DELETE", f"/api/v1/tools/id/{tool_id}/delete")

    def list_models(self) -> list[str]:
        """Every model id Open WebUI currently registers, as plain strings.

        This is the id space the ``?models=`` pin is checked against: Open
        WebUI only pins a model whose id exactly matches one of these. For an
        OpenAI-compatible provider these are the ids the upstream ``/v1/models``
        endpoint returns, which may differ from Studio's own ``model_id``.
        """
        payload = self.request("GET", "/api/models")
        models = payload.get("data") if isinstance(payload, dict) else payload
        return [
            str(model["id"])
            for model in (models or [])
            if isinstance(model, dict) and model.get("id")
        ]



# --- pure helpers (no I/O, so they are cheap to test) ----------------------
def chat_model_prefix(connection) -> str:
    """The Open WebUI ``prefix_id`` for a connection.

    Open WebUI identifies a model by ``<prefix_id>.<model_id>`` and strips the
    prefix before forwarding the request upstream. Without a prefix, the same
    ``model_id`` registered under two different connections collides in Open
    WebUI's model list (one silently shadows the other). Keying the prefix on
    the connection's primary key makes every pushed model id globally unique
    while the upstream request still carries the bare model id.
    """
    return str(connection.id)


def connection_payload(conn) -> dict[str, Any]:
    """The part of a Studio ModelConnection that Open WebUI needs.

    ``model_ids`` narrows the connection to the models Studio has registered
    under it; empty means Studio has registered none, and Open WebUI then offers
    whatever the provider lists. ``prefix_id`` namespaces those ids so the same
    model id under two connections does not collide in Open WebUI.
    """
    from model_registry.services import connection_api_key

    return {
        "id": conn.id,
        "name": conn.name,
        "base_url": (conn.base_url or "").strip().rstrip("/"),
        "api_key": connection_api_key(conn),
        "enabled": conn.enabled,
        "project_slug": conn.project.slug,
        "model_ids": sorted(
            conn.models.filter(enabled=True).values_list("model_id", flat=True).distinct()
        ),
        "prefix_id": chat_model_prefix(conn),
    }


def plan_openai_config(current: dict[str, Any], connections: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge Studio's connections into Open WebUI's OpenAI provider config.

    Entries Studio pushed before (they carry ``STUDIO_MARKER``) are replaced;
    entries somebody added in Open WebUI itself are kept, in their order, with
    their config re-keyed to their new index.

    Open WebUI stores the three structures as parallel, index-aligned lists,
    but a hand edit or a partially failed write can leave them out of sync.
    The merge therefore pairs strictly by index over the union of the indices
    present in any of the three: a missing key defaults to ``""`` and a
    missing config to ``{}``, so a key can never end up attached to a URL
    from a different index. An entry is kept only if it has a base URL — a
    bare key or config with no URL is dropped, since a URL is what makes a
    provider usable.
    """
    urls = list(current.get("OPENAI_API_BASE_URLS") or [])
    keys = list(current.get("OPENAI_API_KEYS") or [])
    configs = dict(current.get("OPENAI_API_CONFIGS") or {})

    indices = set(range(len(urls))) | set(range(len(keys)))
    for config_key in configs:
        if config_key.isdigit() and str(int(config_key)) == config_key:
            indices.add(int(config_key))

    kept = []
    for index in sorted(indices):
        url = urls[index] if index < len(urls) else None
        if url is None:
            continue  # a bare key or config with no URL is not a usable provider
        api_key = keys[index] if index < len(keys) else ""
        raw_config = configs.get(str(index))
        config = raw_config if isinstance(raw_config, dict) else {}
        if STUDIO_MARKER in config:
            continue
        kept.append((url, api_key, config))
    ours = [
        (
            connection["base_url"],
            connection.get("api_key", ""),
            {
                STUDIO_MARKER: connection["id"],
                "enable": bool(connection.get("enabled", True)),
                # Shown in Open WebUI's admin UI, so it reads as the Studio name.
                "name": connection["name"],
                # Open WebUI treats an empty list as "no restriction".
                "model_ids": list(connection.get("model_ids") or []),
                # Namespaces the model ids so the same id under two connections
                # does not collide in Open WebUI's model list.
                "prefix_id": connection.get("prefix_id"),
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
