"""OpenWebUI adapter — the single boundary between Studio and OpenWebUI.

All OpenWebUI-specific HTTP/API logic lives here. The rest of Studio depends
on its own Agent/Knowledge/Tool models, not OpenWebUI response dictionaries.

The adapter reuses ``chat.api.ChatAPI`` for authentication and basic HTTP,
wrapping it with higher-level operations that map Studio domain objects to
OpenWebUI API calls.
"""
from __future__ import annotations

import logging
from typing import Any

from chat.api import ChatAPI, ChatAPIError, chat_model_prefix
from model_registry.models import (
    Agent,
    KnowledgeBase,
    Tool,
)

logger = logging.getLogger(__name__)


class OpenWebUIAdapterError(RuntimeError):
    """An OpenWebUI operation failed."""


class OpenWebUIAdapter:
    """High-level operations for configuring and running agents in OpenWebUI.

    Usage::

        adapter = OpenWebUIAdapter.for_user(user)
        models = adapter.list_models()
        kbs = adapter.list_knowledge_bases()
        adapter.create_or_update_agent(agent)
    """

    def __init__(self, api: ChatAPI):
        self._api = api

    @classmethod
    def for_user(cls, user) -> OpenWebUIAdapter:
        api = ChatAPI.as_user(user)
        return cls(api)

    # --- read operations ---------------------------------------------------

    def list_models(self) -> list[str]:
        """All model ids registered in OpenWebUI."""
        return self._api.list_models()

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        """All knowledge bases visible to the current user, as plain dicts."""
        return self._api.knowledge_bases()

    def list_tools(self) -> list[dict[str, Any]]:
        """All toolkit tools registered in OpenWebUI.

        Toolkit tools (the Tools page) are served by ``/api/v1/tools/``;
        ``/api/v1/functions/`` is for pipes/filters/actions and would miss
        real tools. Their description lives in ``meta.manifest`` (the
        frontmatter of the module docstring).
        """
        try:
            items = self._api.tools()
        except ChatAPIError:
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            manifest = meta.get("manifest") if isinstance(meta.get("manifest"), dict) else {}
            out.append(
                {
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "description": manifest.get("description")
                    or meta.get("description")
                    or "",
                    "kind": "tool",
                }
            )
        return out

    # --- agent configuration ------------------------------------------------
    #
    # OpenWebUI's "agent" primitive is a workspace *model* entry: a model id
    # that inherits a base model and carries ``meta.knowledge`` (the attached
    # knowledge bases). Studio agents are therefore materialized as such
    # entries — created/updated on every Studio create/edit, and readable via
    # ``agent_remote`` for the detail page. OpenWebUI is the source of truth
    # for the live config; Studio keeps the row + ``external_id`` as the
    # durable, audit-relevant reference.

    AGENT_MODEL_PREFIX = "studio.agent"

    def agent_model_id(self, agent: Agent) -> str:
        """The deterministic OpenWebUI model id for a Studio agent.

        ``studio.agent-<pk>``: stable across renames, unique per agent, and
        the ``-<pk>`` suffix keeps it distinct from base model ids.
        """
        return f"{self.AGENT_MODEL_PREFIX}-{agent.pk}"

    def agent_base_model_id(self, agent: Agent) -> str:
        """The OpenWebUI id of the agent's base (pushed) model."""
        model = agent.base_model
        return f"{chat_model_prefix(model.connection)}.{model.model_id}"

    def _knowledge_refs(self, agent: Agent) -> list[dict[str, Any]]:
        """``meta.knowledge`` entries: file-shaped refs to the agent's KBs.

        The OpenWebUI chat runtime reads ``meta.knowledge`` at completion time
        and treats each ``{"id", "name", "type": "file"}`` entry as a
        knowledge source. KBs without an OpenWebUI id are not linkable yet.
        """
        return [
            {"id": kb.external_id, "name": kb.name, "type": "file"}
            for kb in agent.knowledge_bases.all()
            if kb.external_id
        ]

    def push_agent(self, agent: Agent) -> dict[str, Any]:
        """Create or update the agent's OpenWebUI model entry.

        Idempotent: create when ``external_id`` is empty, update when it is
        set, and recover a stale create (entry deleted in OpenWebUI) by
        recreating. Backfills ``agent.external_id`` on success.

        Returns ``{"status": "created"|"updated"|"unchanged"}``. Raises
        ``OpenWebUIAdapterError`` on failure — callers must treat sync as
        best-effort and surface it without breaking the Studio request.
        """
        model_id = self.agent_model_id(agent)
        base_model_id = self.agent_base_model_id(agent)
        knowledge = self._knowledge_refs(agent)
        description = agent.system_prompt or agent.description or agent.name
        metadata = {
            "simpleaudit": {
                "retrieval_profile": (
                    agent.retrieval_profile.config_dict()
                    if agent.retrieval_profile else None
                ),
            },
        }

        try:
            if agent.external_id:
                try:
                    self._api.update_workspace_model(
                        model_id, agent.name, base_model_id=base_model_id,
                        description=description, knowledge=knowledge,
                        metadata=metadata,
                    )
                    status = "updated"
                except ChatAPIError as exc:
                    # A 404 means the entry vanished in OpenWebUI — recreate
                    # it with the same deterministic id.
                    if "404" not in str(exc) and "not found" not in str(exc).lower():
                        raise
                    self._api.create_workspace_model(
                        model_id, agent.name, base_model_id=base_model_id,
                        description=description, knowledge=knowledge,
                        metadata=metadata,
                    )
                    status = "created"
            else:
                try:
                    self._api.create_workspace_model(
                        model_id, agent.name, base_model_id=base_model_id,
                        description=description, knowledge=knowledge,
                        metadata=metadata,
                    )
                    status = "created"
                except ChatAPIError as exc:
                    if "already registered" not in str(exc).lower():
                        raise
                    # Someone (an earlier crashed run) already created it.
                    self._api.update_workspace_model(
                        model_id, agent.name, base_model_id=base_model_id,
                        description=description, knowledge=knowledge,
                        metadata=metadata,
                    )
                    status = "updated"
        except ChatAPIError as exc:
            raise OpenWebUIAdapterError(
                f"Could not sync agent '{agent.name}' to OpenWebUI: {exc}"
            ) from exc

        if agent.external_id != model_id:
            agent.external_id = model_id
            agent.save(update_fields=["external_id", "updated_at"])

        logger.info(
            "Agent '%s' synced to OpenWebUI model %s (%s)",
            agent.name, model_id, status,
        )
        return {"status": status, "model_id": model_id}

    def agent_remote(self, agent: Agent) -> dict[str, Any] | None:
        """Fetch the live OpenWebUI config for an agent, or None.

        ``None`` means "no live view" (no entry yet, OpenWebUI down, or the
        entry was removed there) — callers fall back to the local cached row.
        """
        if not agent.external_id:
            return None
        try:
            item = self._api.get_workspace_model(agent.external_id)
        except ChatAPIError:
            return None
        if not isinstance(item, dict):
            return None
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        return {
            "model_id": item.get("id", ""),
            "name": item.get("name", ""),
            "base_model_id": item.get("base_model_id"),
            "description": meta.get("description") or "",
            "knowledge": meta.get("knowledge") or [],
            "is_active": item.get("is_active", True),
        }

    def delete_agent(self, agent: Agent) -> None:
        """Delete the agent's OpenWebUI model entry (best-effort, never raises)."""
        if not agent.external_id:
            return
        try:
            self._api.delete_workspace_model(agent.external_id)
        except ChatAPIError as exc:
            logger.warning(
                "Agent '%s': OpenWebUI model delete failed (%s); local row deleted anyway.",
                agent.name, exc,
            )

    def resolve_agent_config(self, agent: Agent) -> dict[str, Any]:
        """The resolved execution config (used by the chat layer).

        Kept as a separate, side-effect-free read so the execution path never
        triggers a push.
        """
        kb_ids = [kb.external_id for kb in agent.knowledge_bases.all() if kb.external_id]
        tool_ids = [tool.external_id for tool in agent.tools.all() if tool.external_id]
        return {
            "model_id": agent.external_id or self.agent_model_id(agent),
            "base_model_id": self.agent_base_model_id(agent),
            "system_prompt": agent.system_prompt,
            "knowledge_base_ids": kb_ids,
            "tool_ids": tool_ids,
            "retrieval_profile": (
                agent.retrieval_profile.config_dict()
                if agent.retrieval_profile
                else None
            ),
            "capabilities": agent.capabilities,
        }

    # Backward-compatible alias: the old name implied push without one.
    def create_or_update_agent(self, agent: Agent) -> dict[str, Any]:
        """Sync the agent to OpenWebUI and return its execution config."""
        self.push_agent(agent)
        return self.resolve_agent_config(agent)

    def run_agent(self, agent: Agent, messages: list[dict[str, str]], *, session_id: str | None = None) -> dict[str, Any]:
        """Execute a conversation through OpenWebUI using the agent's config.

        This is the execution path: Studio → adapter → OpenWebUI runtime.
        The messages are standard chat format ``[{"role": ..., "content": ...}]``.

        Side-effect-free: it resolves the agent's model entry but never
        pushes. When the agent has a synced OpenWebUI model entry that id is
        used (its ``meta.knowledge`` gives the RAG context); otherwise the
        raw base model id is used with explicit knowledge/tool ids.
        """
        config = self.resolve_agent_config(agent)
        if agent.external_id:
            model_id = agent.external_id
        else:
            model_id = config["base_model_id"]

        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "stream": False,
        }
        if agent.system_prompt:
            payload["system"] = agent.system_prompt
        if session_id:
            payload["chat_id"] = session_id

        # Knowledge base ids for RAG
        if config["knowledge_base_ids"]:
            payload["knowledge_base_ids"] = config["knowledge_base_ids"]

        # Tool ids
        if config["tool_ids"]:
            payload["tool_ids"] = config["tool_ids"]

        try:
            response = self._api.request("POST", "/api/v1/chat/completions", json=payload)
        except ChatAPIError as exc:
            raise OpenWebUIAdapterError(f"Agent execution failed: {exc}") from exc

        return {
            "response": response,
            "model_id": model_id,
            "config": config,
        }

    # --- sync helpers -------------------------------------------------------

    def sync_knowledge_bases(self, kbs: list[KnowledgeBase]) -> list[dict[str, Any]]:
        """Pull the latest state of the given knowledge bases from OpenWebUI.

        Updates ``external_id`` and ``file_count`` on the Studio-side rows.
        Returns the updated list.
        """
        remote = {kb["id"]: kb for kb in self.list_knowledge_bases()}
        updated = []
        for kb in kbs:
            if kb.external_id and kb.external_id in remote:
                r = remote[kb.external_id]
                kb.description = r.get("description", kb.description)
                kb.save(update_fields=["description", "updated_at"])
            updated.append(kb)
        return updated

    def sync_tools(self, tools: list[Tool]) -> list[dict[str, Any]]:
        """Pull the latest state of the given tools from OpenWebUI."""
        remote = {t["name"]: t for t in self.list_tools()}
        updated = []
        for tool in tools:
            if tool.name in remote:
                r = remote[tool.name]
                tool.description = r.get("description", tool.description)
                tool.save(update_fields=["description", "updated_at"])
            updated.append(tool)
        return updated
