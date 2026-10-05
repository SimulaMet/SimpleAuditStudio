"""Sync behavior between Studio's agent/KB/tool domain and Open WebUI.

With chat disabled (the default in tests) the API behaves exactly as before
and never touches Open WebUI. With chat enabled, every create/update/delete
propagates to Open WebUI: agents become workspace models (``studio.agent-<pk>``),
knowledge bases and tools are created/updated/deleted through the adapter.
Failures must never break the Studio API call itself.
"""
import json
from unittest import mock

from django.test import Client, TestCase

from chat.api import ChatAPIError
from infra.tests.factories import (
    AgentFactory,
    KnowledgeBaseFactory,
    MembershipFactory,
    ProjectFactory,
    RegisteredModelFactory,
    ToolFactory,
    UserFactory,
)
from model_registry.models import Agent, KnowledgeBase, RegisteredModel, Tool


class _SyncBase(TestCase):
    """Shared fixture: one user/project with the API client ready."""

    def setUp(self):
        self.user = UserFactory(is_superuser=True, is_staff=True)
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.force_login(self.user)
        self.client.session["active_project_id"] = self.project.id
        self.client.session.save()

    def _api(self, path, method="get", data=None):
        if method == "get":
            return self.client.get(path)
        if method == "post":
            return self.client.post(path, data=json.dumps(data), content_type="application/json")
        if method == "put":
            return self.client.put(path, data=json.dumps(data), content_type="application/json")
        return self.client.delete(path)

    def _mock_chat(self):
        """Chat enabled + a mocked ChatAPI. Returns the mock api instance."""
        enabled = mock.patch("chat.config.ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        api = mock.Mock()
        api.get_workspace_model.return_value = None
        patcher = mock.patch("chat.api.ChatAPI.as_user", return_value=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        return api


class _ChatDisabledBase(_SyncBase):
    def setUp(self):
        super().setUp()
        enabled = mock.patch("chat.config.ENABLED", False)
        enabled.start()
        self.addCleanup(enabled.stop)


class ChatDisabledNoOpTest(_ChatDisabledBase):
    def test_agent_crud_never_touches_openwebui(self):
        model = RegisteredModelFactory(project=self.project)
        with mock.patch("chat.api.ChatAPI.as_user") as as_user:
            resp = self._api("/api/agents/", "post", {
                "name": "Plain Agent", "base_model": model.id,
            })
            self.assertEqual(resp.status_code, 201)
            agent = Agent.objects.get(name="Plain Agent")
            self._api(f"/api/agents/{agent.id}/", "put", {
                "name": "Plain Agent v2", "base_model": model.id,
            })
            self._api(f"/api/agents/{agent.id}/", "delete")
            self._api("/api/knowledge-bases/", "post", {"name": "Plain KB"})
            self._api("/api/tools/", "post", {"name": "Plain Tool", "type": "builtin"})
        as_user.assert_not_called()

    def test_snapshot_still_freezes_without_external_id(self):
        agent = AgentFactory(project=self.project)
        resp = self._api(f"/api/agents/{agent.id}/snapshot/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["openwebui_model_id"], "")


class AgentSyncTest(_SyncBase):
    def test_pushes_openwebui_capabilities_and_builtin_tools(self):
        api = self._mock_chat()
        agent = AgentFactory(
            project=self.project,
            capabilities={
                "model": {"file_context": True, "web_search": False},
                "builtin_tools": {"memory": False, "notifications": True},
            },
        )
        agent.external_id = f"studio.agent-{agent.pk}"
        agent.save(update_fields=["external_id"])
        agent.knowledge_bases.add(
            KnowledgeBaseFactory(project=self.project, external_id="owui-kb-1")
        )
        agent.tools.add(
            ToolFactory(project=self.project, external_id="owui-tool-1")
        )

        resp = self._api(f"/api/agents/{agent.id}/", "put", {
            "name": agent.name,
            "base_model": agent.base_model.id,
        })

        self.assertEqual(resp.status_code, 200)
        metadata = api.update_workspace_model.call_args.kwargs["metadata"]
        self.assertTrue(metadata["capabilities"]["file_context"])
        self.assertFalse(metadata["capabilities"]["web_search"])
        self.assertFalse(metadata["builtinTools"]["knowledge"])
        self.assertFalse(metadata["builtinTools"]["memory"])
        self.assertFalse(metadata["builtinTools"]["notifications"])
        self.assertEqual(metadata["toolIds"], ["owui-tool-1"])

    def test_pushes_agent_retrieval_settings_as_studio_metadata(self):
        api = self._mock_chat()
        settings = {"search_mode": "hybrid", "top_k": 9, "rerank_enabled": True, "full_context": True}
        agent = AgentFactory(project=self.project, retrieval_settings=settings)

        resp = self._api(f"/api/agents/{agent.id}/", "put", {
            "name": agent.name,
            "base_model": agent.base_model.id,
            "retrieval_settings": settings,
        })

        self.assertEqual(resp.status_code, 200)
        pushed = api.create_workspace_model.call_args.kwargs["metadata"]
        self.assertEqual(pushed["simpleaudit"]["retrieval"]["top_k"], 9)
        self.assertTrue(pushed["simpleaudit"]["retrieval"]["rerank_enabled"])
        self.assertEqual(api.create_workspace_model.call_args.kwargs["params"], settings)

    def test_create_pushes_workspace_model_and_backfills_id(self):
        api = self._mock_chat()
        model = RegisteredModelFactory(project=self.project)
        kb = KnowledgeBaseFactory(project=self.project, external_id="owui-kb-1")
        resp = self._api("/api/agents/", "post", {
            "name": "Synced Agent",
            "base_model": model.id,
            "system_prompt": "Be precise.",
            "description": "Synced agent",
            "knowledge_bases": [kb.id],
        })
        self.assertEqual(resp.status_code, 201)
        agent = Agent.objects.get(name="Synced Agent")
        self.assertEqual(agent.external_id, f"studio.agent-{agent.pk}")
        agent_model = RegisteredModel.objects.get(
            project=self.project, model_id=agent.external_id
        )
        self.assertEqual(agent_model.display_name, "Synced Agent")
        api.create_workspace_model.assert_called_once()
        call = api.create_workspace_model.call_args
        self.assertEqual(call.args[0], f"studio.agent-{agent.pk}")
        self.assertEqual(call.args[1], "Synced Agent")
        self.assertEqual(call.kwargs["base_model_id"], f"{model.connection_id}.{model.model_id}")
        self.assertEqual(
            call.kwargs["knowledge"], [{"id": "owui-kb-1", "name": kb.name, "type": "file"}]
        )
        # The pushed config points at the OWUI model, so chat can pin it.
        self.assertEqual(agent.config_snapshot()["openwebui_model_id"], agent.external_id)

    def test_create_without_external_knowledge_omits_meta_knowledge(self):
        api = self._mock_chat()
        model = RegisteredModelFactory(project=self.project)
        resp = self._api("/api/agents/", "post", {
            "name": "Bare Agent", "base_model": model.id,
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(api.create_workspace_model.call_args.kwargs["knowledge"], [])

    def test_update_repushes_existing_workspace_model(self):
        api = self._mock_chat()
        agent = AgentFactory(project=self.project, external_id="studio.agent-1")
        resp = self._api(f"/api/agents/{agent.id}/", "put", {
            "name": "Renamed",
            "base_model": agent.base_model.id,
            "system_prompt": "New prompt",
        })
        self.assertEqual(resp.status_code, 200)
        api.update_workspace_model.assert_called_once()
        self.assertEqual(api.update_workspace_model.call_args.args[0], "studio.agent-1")
        api.create_workspace_model.assert_not_called()

    def test_delete_removes_workspace_model(self):
        api = self._mock_chat()
        agent = AgentFactory(project=self.project, external_id="studio.agent-1")
        resp = self._api(f"/api/agents/{agent.id}/", "delete")
        self.assertEqual(resp.status_code, 204)
        api.delete_workspace_model.assert_called_once_with("studio.agent-1")

    def test_unsynced_agent_create_retries_as_update_when_id_taken(self):
        api = self._mock_chat()
        api.create_workspace_model.side_effect = ChatAPIError(
            "Uh-oh! This model id is already registered."
        )
        model = RegisteredModelFactory(project=self.project)
        resp = self._api("/api/agents/", "post", {
            "name": "Retried Agent", "base_model": model.id,
        })
        self.assertEqual(resp.status_code, 201)
        api.update_workspace_model.assert_called_once()

    def test_update_404_recreates_missing_remote_model(self):
        """A synced agent whose entry was deleted in OpenWebUI is recreated."""
        api = self._mock_chat()
        api.update_workspace_model.side_effect = ChatAPIError("404 Internal Server Error")
        agent = AgentFactory(project=self.project)
        agent.external_id = f"studio.agent-{agent.pk}"
        agent.save()
        resp = self._api(f"/api/agents/{agent.id}/", "put", {
            "name": agent.name, "base_model": agent.base_model.id,
            "system_prompt": agent.system_prompt,
        })
        self.assertEqual(resp.status_code, 200)
        api.create_workspace_model.assert_called_once()
        self.assertEqual(api.create_workspace_model.call_args.args[0], agent.external_id)

    def test_sync_failure_does_not_block_agent_save(self):
        api = self._mock_chat()
        api.create_workspace_model.side_effect = ChatAPIError("connection refused")
        model = RegisteredModelFactory(project=self.project)
        resp = self._api("/api/agents/", "post", {
            "name": "Durable Agent", "base_model": model.id,
        })
        self.assertEqual(resp.status_code, 201)
        agent = Agent.objects.get(name="Durable Agent")
        self.assertEqual(agent.external_id, "")


class AgentLiveFetchTest(_SyncBase):
    """The detail endpoint returns the live Open WebUI entry when available."""

    def test_detail_includes_live_entry_when_synced(self):
        api = self._mock_chat()
        agent = AgentFactory(project=self.project, external_id="studio.agent-7")
        api.get_workspace_model.return_value = {
            "id": "studio.agent-7",
            "name": agent.name,
            "base_model_id": "1.model-0",
            "meta": {"description": "d", "knowledge": [{"id": "kb-1"}]},
            "is_active": True,
        }
        resp = self._api(f"/api/agents/{agent.id}/")
        self.assertEqual(resp.status_code, 200)
        live = resp.json()["openwebui_live"]
        self.assertEqual(live["model_id"], "studio.agent-7")
        self.assertEqual(live["knowledge"], [{"id": "kb-1"}])

    def test_detail_is_null_when_remote_unreachable(self):
        api = self._mock_chat()
        agent = AgentFactory(project=self.project, external_id="studio.agent-7")
        api.get_workspace_model.side_effect = ChatAPIError("owui down")
        resp = self._api(f"/api/agents/{agent.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["openwebui_live"])

    def test_detail_is_null_when_never_synced(self):
        api = self._mock_chat()
        agent = AgentFactory(project=self.project)
        resp = self._api(f"/api/agents/{agent.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["openwebui_live"])
        api.get_workspace_model.assert_not_called()

    def test_detail_returns_tool_ids_from_live_workspace_model(self):
        """Regression: workspace model toolIds must be extracted and returned."""
        api = self._mock_chat()
        agent = AgentFactory(project=self.project, external_id="studio.agent-7")
        api.get_workspace_model.return_value = {
            "id": "studio.agent-7",
            "name": agent.name,
            "base_model_id": "1.model-0",
            "meta": {
                "description": "d",
                "knowledge": [{"id": "kb-1"}],
                "toolIds": ["owui-tool-1", "owui-tool-2"],
            },
            "is_active": True,
        }
        resp = self._api(f"/api/agents/{agent.id}/")
        self.assertEqual(resp.status_code, 200)
        live = resp.json()["openwebui_live"]
        self.assertEqual(live["tool_ids"], ["owui-tool-1", "owui-tool-2"])

    def test_list_does_not_fetch_live(self):
        api = self._mock_chat()
        AgentFactory(project=self.project, external_id="studio.agent-7")
        resp = self._api("/api/agents/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()[0]["openwebui_live"])
        api.get_workspace_model.assert_not_called()

    def test_chat_disabled_detail_is_null(self):
        enabled = mock.patch("chat.config.ENABLED", False)
        enabled.start()
        self.addCleanup(enabled.stop)
        api = mock.Mock()
        patcher = mock.patch("chat.api.ChatAPI.as_user", return_value=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        agent = AgentFactory(project=self.project, external_id="studio.agent-7")
        resp = self._api(f"/api/agents/{agent.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["openwebui_live"])
        api.get_workspace_model.assert_not_called()


class KnowledgeBaseSyncTest(_SyncBase):
    def test_create_pushes_knowledge_base(self):
        api = self._mock_chat()
        api.create_knowledge_base.return_value = {"id": "kb-new-1"}
        resp = self._api("/api/knowledge-bases/", "post", {
            "name": "HR Policies", "description": "HR docs",
        })
        self.assertEqual(resp.status_code, 201)
        api.create_knowledge_base.assert_called_once_with("HR Policies", "HR docs")
        kb = KnowledgeBase.objects.get(name="HR Policies")
        self.assertEqual(kb.external_id, "kb-new-1")

    def test_update_pushes_knowledge_base_change(self):
        api = self._mock_chat()
        kb = KnowledgeBaseFactory(project=self.project, external_id="kb-1")
        resp = self._api(f"/api/knowledge-bases/{kb.id}/", "put", {
            "name": "HR Policies v2", "description": "Updated",
        })
        self.assertEqual(resp.status_code, 200)
        api.update_knowledge_base.assert_called_once_with("kb-1", "HR Policies v2", "Updated")

    def test_delete_propagates_to_openwebui(self):
        api = self._mock_chat()
        kb = KnowledgeBaseFactory(project=self.project, external_id="kb-1")
        resp = self._api(f"/api/knowledge-bases/{kb.id}/", "delete")
        self.assertEqual(resp.status_code, 204)
        api.delete_knowledge_base.assert_called_once_with("kb-1")

    def test_sync_failure_still_creates_local_row(self):
        api = self._mock_chat()
        api.create_knowledge_base.side_effect = ChatAPIError("owui down")
        resp = self._api("/api/knowledge-bases/", "post", {"name": "Local KB"})
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(
            KnowledgeBase.objects.get(name="Local KB").external_id
        )


class ToolSyncTest(_SyncBase):
    def test_create_with_content_pushes_tool(self):
        api = self._mock_chat()
        api.create_tool.return_value = {"id": "tool-abc"}
        resp = self._api("/api/tools/", "post", {
            "name": "Refund Search",
            "type": "custom",
            "content": "def refund_search(order_id: str):\n    return []",
        })
        self.assertEqual(resp.status_code, 201)
        api.create_tool.assert_called_once()
        args, _ = api.create_tool.call_args
        tool = Tool.objects.get(name="Refund Search")
        self.assertEqual(args[0], f"studio.tool-{tool.pk}")
        self.assertEqual(args[1], "Refund Search")
        self.assertEqual(args[2], "def refund_search(order_id: str):\n    return []")
        # Studio never persists tool source (Open WebUI owns it).
        self.assertNotIn("content", resp.json())

    def test_create_without_content_stays_local(self):
        api = self._mock_chat()
        resp = self._api("/api/tools/", "post", {"name": "Local Tool", "type": "custom"})
        self.assertEqual(resp.status_code, 201)
        api.create_tool.assert_not_called()

    def test_metadata_only_update_fetches_and_repushes_remote_source(self):
        api = self._mock_chat()
        tool = ToolFactory(project=self.project, external_id="tool-abc")
        api.get_tool.return_value = {"id": "tool-abc", "name": "Refund Search", "content": "src"}
        resp = self._api(f"/api/tools/{tool.id}/", "put", {
            "name": "Refund Search v2", "type": "custom",
        })
        self.assertEqual(resp.status_code, 200)
        api.get_tool.assert_called_once_with("tool-abc")
        api.update_tool.assert_called_once()
        args, _ = api.update_tool.call_args
        self.assertEqual(args[0], "tool-abc")
        self.assertEqual(args[2], "src")

    def test_update_with_new_content_pushes_it(self):
        api = self._mock_chat()
        tool = ToolFactory(project=self.project, external_id="tool-abc")
        resp = self._api(f"/api/tools/{tool.id}/", "put", {
            "name": tool.name, "type": "custom", "content": "new source",
        })
        self.assertEqual(resp.status_code, 200)
        api.update_tool.assert_called_once()
        self.assertEqual(api.update_tool.call_args.args[2], "new source")

    def test_update_of_unsynced_tool_stays_local(self):
        api = self._mock_chat()
        tool = ToolFactory(project=self.project)
        resp = self._api(f"/api/tools/{tool.id}/", "put", {
            "name": tool.name, "type": "custom",
        })
        self.assertEqual(resp.status_code, 200)
        api.get_tool.assert_not_called()
        api.update_tool.assert_not_called()

    def test_delete_propagates_to_openwebui(self):
        api = self._mock_chat()
        tool = ToolFactory(project=self.project, external_id="tool-abc")
        resp = self._api(f"/api/tools/{tool.id}/", "delete")
        self.assertEqual(resp.status_code, 204)
        api.delete_tool.assert_called_once_with("tool-abc")
