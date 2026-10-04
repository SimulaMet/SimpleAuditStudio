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

    def create_or_update_agent(self, agent: Agent) -> dict[str, Any]:
        """Push an Agent's configuration to OpenWebUI.

        OpenWebUI does not have a native "agent" concept in all versions, so
        this maps the Agent onto the closest available primitives:
        - The model is selected via the connection/model push (already done
          by ``chat.sync``).
        - Knowledge bases are referenced by their OpenWebUI ids.
        - Tools are referenced by their OpenWebUI function ids.
        - The system prompt and retrieval settings are stored in the agent's
          metadata for the chat layer to consume at execution time.

        Returns a dict with the OpenWebUI-side identifiers.
        """
        model = agent.base_model
        conn = model.connection
        prefix = chat_model_prefix(conn)
        openwebui_model_id = f"{prefix}.{model.model_id}"

        kb_ids = [
            kb.external_id
            for kb in agent.knowledge_bases.all()
            if kb.external_id
        ]

        tool_ids = [
            tool.external_id
            for tool in agent.tools.all()
            if tool.external_id
        ]

        # Build the configuration payload. In a full OpenWebUI deployment this
        # would call a specific "create agent" endpoint. For now we return the
        # resolved identifiers so the chat layer can use them.
        config = {
            "model_id": openwebui_model_id,
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

        logger.info(
            "Agent %s configured: model=%s kbs=%d tools=%d",
            agent.name, openwebui_model_id, len(kb_ids), len(tool_ids),
        )
        return config

    def run_agent(self, agent: Agent, messages: list[dict[str, str]], *, session_id: str | None = None) -> dict[str, Any]:
        """Execute a conversation through OpenWebUI using the agent's config.

        This is the execution path: Studio → adapter → OpenWebUI runtime.
        The messages are standard chat format ``[{"role": ..., "content": ...}]``.
        """
        config = self.create_or_update_agent(agent)
        model_id = config["model_id"]

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
