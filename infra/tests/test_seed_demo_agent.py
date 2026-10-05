"""Tests for the demo support-agent seed (infra.seed.seed_demo_agent)."""

from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Project
from infra.seed import DEMO_AGENT, backfill_demo_chat_resources, seed_demo_agent
from infra.tests.factories import (
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)
from infra.tests.utils import PASSWORD, safe_env
from model_registry.models import Agent, KnowledgeBase, Tool


def _project_with_model():
    user = UserFactory(is_superuser=True, is_staff=True)
    user.set_password(PASSWORD)
    user.save()
    project = ProjectFactory()
    model = RegisteredModelFactory(project=project, display_name="GPT-4o", model_id="gpt-4o")
    return user, project, model


class SeedDemoAgentLocalTests(TestCase):
    """Chat disabled: local reference rows are created, no external ids."""

    def setUp(self):
        self._enabled = mock.patch("chat.config.ENABLED", False)
        self._enabled.start()
        self.addCleanup(self._enabled.stop)
        self.user, self.project, self.model = _project_with_model()

    def test_creates_agent_kb_tool_profile_and_links(self):
        status = seed_demo_agent(self.project, self.user)

        self.assertEqual(status["agent"], "created")
        self.assertEqual(status["openwebui"], "disabled")

        agent = Agent.objects.get(project=self.project, name=DEMO_AGENT["name"])
        self.assertEqual(agent.base_model_id, self.model.id)
        self.assertEqual(agent.created_by_id, self.user.id)
        self.assertTrue(agent.capabilities["knowledge_search"])

        kb = KnowledgeBase.objects.get(project=self.project, name=DEMO_AGENT["knowledge_base"]["name"])
        self.assertEqual(kb.external_id, "")
        kb_ids = list(agent.knowledge_bases.values_list("id", flat=True))
        self.assertEqual(kb_ids, [kb.id])

        tool = Tool.objects.get(project=self.project, name=DEMO_AGENT["tool"]["name"])
        self.assertEqual(tool.external_id, "")
        self.assertEqual(tool.read_only, True)
        self.assertEqual(tool.type, "custom")
        tool_ids = list(agent.tools.values_list("id", flat=True))
        self.assertEqual(tool_ids, [tool.id])

        self.assertEqual(agent.retrieval_settings["search_mode"], "hybrid")

    def test_is_idempotent(self):
        seed_demo_agent(self.project, self.user)
        status = seed_demo_agent(self.project, self.user)

        self.assertEqual(status["agent"], "reused")
        self.assertEqual(Agent.objects.filter(project=self.project).count(), 1)
        self.assertEqual(KnowledgeBase.objects.filter(project=self.project).count(), 1)
        self.assertEqual(Tool.objects.filter(project=self.project).count(), 1)
        self.assertEqual(Agent.objects.get(project=self.project).retrieval_settings["top_k"], 5)

    def test_skips_when_no_model_exists(self):
        self.model.delete()
        status = seed_demo_agent(self.project, self.user)

        self.assertEqual(status["agent"], "no-model")
        self.assertEqual(Agent.objects.filter(project=self.project).count(), 0)


class SeedDemoAgentPushTests(TestCase):
    """Chat enabled: the seed pushes KB + docs + tool to Open WebUI."""

    def setUp(self):
        self._enabled = mock.patch("chat.config.ENABLED", True)
        self._enabled.start()
        self.addCleanup(self._enabled.stop)
        self.user, self.project, self.model = _project_with_model()

    def _patched_api(self):
        api = mock.Mock()
        api.knowledge_bases.return_value = []
        api.create_knowledge_base.return_value = {"id": "kb-abc"}
        api.upload_file.side_effect = lambda name, content, ct: {"id": f"file-{name}"}
        api.tools.return_value = []
        return api

    def test_pushes_and_sets_external_ids(self):
        api = self._patched_api()
        with mock.patch("chat.api.ChatAPI.as_user", return_value=api) as as_user:
            status = seed_demo_agent(self.project, self.user)

        self.assertEqual(status["openwebui"], "pushed")
        as_user.assert_called_once_with(self.user)
        api.create_knowledge_base.assert_called_once()
        # Two documents uploaded and linked.
        self.assertEqual(api.upload_file.call_count, 2)
        self.assertEqual(api.add_file_to_knowledge_base.call_count, 2)
        api.create_tool.assert_called_once()
        self.assertEqual(api.create_tool.call_args.args[0], DEMO_AGENT["tool"]["owui_tool_id"])

        kb = KnowledgeBase.objects.get(project=self.project, name=DEMO_AGENT["knowledge_base"]["name"])
        self.assertEqual(kb.external_id, "kb-abc")
        tool = Tool.objects.get(project=self.project, name=DEMO_AGENT["tool"]["name"])
        self.assertEqual(tool.external_id, DEMO_AGENT["tool"]["owui_tool_id"])

    def test_skips_existing_kb_and_function_in_openwebui(self):
        api = mock.Mock()
        api.knowledge_bases.return_value = [{"id": "kb-existing", "name": DEMO_AGENT["knowledge_base"]["name"]}]
        api.upload_file.return_value = {"id": "file-1"}
        api.tools.return_value = [
            {"id": DEMO_AGENT["tool"]["owui_tool_id"], "name": "x"}
        ]
        with mock.patch("chat.api.ChatAPI.as_user", return_value=api):
            seed_demo_agent(self.project, self.user)

        api.create_knowledge_base.assert_not_called()
        api.create_tool.assert_not_called()
        self.assertEqual(api.add_file_to_knowledge_base.call_count, 2)

    def test_unreachable_openwebui_still_creates_local_rows(self):
        from chat.api import ChatAPIError

        with mock.patch("chat.api.ChatAPI.as_user", side_effect=ChatAPIError("no route to host")):
            status = seed_demo_agent(self.project, self.user)

        self.assertEqual(status["openwebui"], "unavailable")
        self.assertEqual(status["agent"], "created")
        agent = Agent.objects.get(project=self.project, name=DEMO_AGENT["name"])
        self.assertEqual(agent.knowledge_bases.count(), 1)
        self.assertEqual(agent.tools.count(), 1)

    def test_failed_push_fills_external_ids_on_rerun(self):
        from chat.api import ChatAPIError

        with mock.patch("chat.api.ChatAPI.as_user", side_effect=ChatAPIError("down")):
            seed_demo_agent(self.project, self.user)
        kb = KnowledgeBase.objects.get(project=self.project, name=DEMO_AGENT["knowledge_base"]["name"])
        self.assertEqual(kb.external_id, "")

        api = self._patched_api()
        # KB not found by name this time, so the seed creates it and learns the id.
        with mock.patch("chat.api.ChatAPI.as_user", return_value=api):
            seed_demo_agent(self.project, self.user)
        kb.refresh_from_db()
        self.assertEqual(kb.external_id, "kb-abc")
        self.assertEqual(Agent.objects.filter(project=self.project).count(), 1)


class SeedPlatformDemoAgentTests(TestCase):
    """seed_platform wires in the demo agent."""

    def setUp(self):
        self._enabled = mock.patch("chat.config.ENABLED", False)
        self._enabled.start()
        self.addCleanup(self._enabled.stop)
        self.user = UserFactory(is_superuser=True, is_staff=True)
        self.user.set_password(PASSWORD)
        self.user.save()
        self.project = Project.objects.create(name="Default", slug="default")

    def test_seed_platform_creates_demo_agent(self):
        with safe_env():
            call_command(
                "seed_platform",
                project=self.project.id,
                skip_packs=True,
                skip_demo_audits=True,
            )

        agent = Agent.objects.filter(project=self.project, name=DEMO_AGENT["name"]).first()
        self.assertIsNotNone(agent)
        self.assertEqual(agent.knowledge_bases.count(), 1)
        self.assertEqual(agent.tools.count(), 1)

    def test_seed_platform_can_skip_demo_agent(self):
        with safe_env():
            call_command(
                "seed_platform",
                project=self.project.id,
                skip_packs=True,
                skip_demo_audits=True,
                skip_demo_agent=True,
            )

        self.assertEqual(Agent.objects.filter(project=self.project, name=DEMO_AGENT["name"]).count(), 0)


class SeedDemoAgentBackfillTests(TestCase):
    """Cold start: chat-off seed leaves local rows; chat startup backfills OWUI.

    Mirrors the fresh-install flow: ``setup_local`` (chat disabled) creates the
    demo rows without external ids, then ``manage.py dev`` brings Open WebUI
    up and ``backfill_demo_chat_resources`` completes the push so the
    OWUI-backed /agents/knowledge/ and /agents/tools/ pages are not empty.
    """

    def setUp(self):
        self.user, self.project, self.model = _project_with_model()

    def _patched_api(self):
        api = mock.Mock()
        api.knowledge_bases.return_value = []
        api.create_knowledge_base.return_value = {"id": "kb-backfill"}
        api.upload_file.side_effect = lambda name, content, ct: {"id": f"file-{name}"}
        api.tools.return_value = []
        return api

    def _seed_with_chat_off(self):
        with mock.patch("chat.config.ENABLED", False):
            seed_demo_agent(self.project, self.user)

    def _kb(self):
        return KnowledgeBase.objects.get(
            project=self.project, name=DEMO_AGENT["knowledge_base"]["name"]
        )

    def _tool(self):
        return Tool.objects.get(project=self.project, name=DEMO_AGENT["tool"]["name"])

    def _agent(self):
        return Agent.objects.get(project=self.project, name=DEMO_AGENT["name"])

    def test_backfills_agent_kb_and_tool_ids(self):
        self._seed_with_chat_off()
        self.assertEqual(self._kb().external_id, "")
        self.assertEqual(self._tool().external_id, "")
        self.assertEqual(self._agent().external_id, "")

        api = self._patched_api()
        with mock.patch("chat.config.ENABLED", True), mock.patch(
            "chat.api.ChatAPI.as_user", return_value=api
        ):
            counts = backfill_demo_chat_resources(self.project, self.user)

        self.assertEqual(counts, {"agents": 1, "knowledge_bases": 1, "tools": 1})
        self.assertEqual(self._kb().external_id, "kb-backfill")
        self.assertEqual(self._tool().external_id, DEMO_AGENT["tool"]["owui_tool_id"])
        agent = self._agent()
        self.assertEqual(agent.external_id, f"studio.agent-{agent.pk}")
        # One push per call: KB created once, two docs uploaded, tool once, and
        # the agent's workspace model created once.
        api.create_knowledge_base.assert_called_once()
        self.assertEqual(api.upload_file.call_count, 2)
        api.create_tool.assert_called_once()
        api.create_workspace_model.assert_called_once()
        # The agent is pushed AFTER its KB/tool ids were backfilled, so its
        # OWUI model is wired to the KB.
        self.assertEqual(
            api.create_workspace_model.call_args.kwargs["knowledge"],
            [{"id": "kb-backfill", "name": DEMO_AGENT["knowledge_base"]["name"], "type": "file"}],
        )

    def test_rewires_agent_when_only_it_was_pushed(self):
        # The agent was pushed (has an id) but its KB/tool were not — the common
        # "edited the agent in the UI" state. Backfill must push the KB/tool and
        # re-push the agent so the links land.
        self._seed_with_chat_off()
        agent = self._agent()
        agent.external_id = f"studio.agent-{agent.pk}"
        agent.save()

        api = self._patched_api()
        with mock.patch("chat.config.ENABLED", True), mock.patch(
            "chat.api.ChatAPI.as_user", return_value=api
        ):
            counts = backfill_demo_chat_resources(self.project, self.user)

        self.assertEqual(counts, {"agents": 0, "knowledge_bases": 1, "tools": 1})
        # Agent already had an id -> update path, and it is re-wired to the KB.
        api.update_workspace_model.assert_called_once()
        api.create_workspace_model.assert_not_called()
        self.assertEqual(
            api.update_workspace_model.call_args.kwargs["knowledge"],
            [{"id": "kb-backfill", "name": DEMO_AGENT["knowledge_base"]["name"], "type": "file"}],
        )

    def test_reconciles_existing_agent_when_ids_present(self):
        self._seed_with_chat_off()
        kb = self._kb()
        kb.external_id = "kb-1"
        kb.save()
        tool = self._tool()
        tool.external_id = DEMO_AGENT["tool"]["owui_tool_id"]
        tool.save()
        agent = self._agent()
        agent.external_id = f"studio.agent-{agent.pk}"
        agent.save()

        api = mock.Mock()
        with mock.patch("chat.config.ENABLED", True), mock.patch(
            "chat.api.ChatAPI.as_user", return_value=api
        ) as as_user:
            counts = backfill_demo_chat_resources(self.project, self.user)

        self.assertEqual(counts, {"agents": 0, "knowledge_bases": 0, "tools": 0})
        # Existing ids do not guarantee that Open WebUI's base_model_id is
        # current, so the workspace model is reconciled in-place.
        self.assertEqual(as_user.call_count, 2)
        as_user.assert_any_call(self.user)
        api.create_knowledge_base.assert_not_called()
        api.create_tool.assert_not_called()
        api.update_workspace_model.assert_called_once()

    def test_noop_when_chat_disabled(self):
        self._seed_with_chat_off()
        with mock.patch("chat.config.ENABLED", False):
            counts = backfill_demo_chat_resources(self.project, self.user)

        self.assertEqual(counts, {"agents": 0, "knowledge_bases": 0, "tools": 0})
        self.assertEqual(self._kb().external_id, "")
        self.assertEqual(self._tool().external_id, "")
        self.assertEqual(self._agent().external_id, "")

    def test_unreachable_openwebui_never_raises_and_retries_later(self):
        from chat.api import ChatAPIError

        self._seed_with_chat_off()
        with mock.patch("chat.config.ENABLED", True), mock.patch(
            "chat.api.ChatAPI.as_user", side_effect=ChatAPIError("no route to host")
        ):
            counts = backfill_demo_chat_resources(self.project, self.user)
        self.assertEqual(counts, {"agents": 0, "knowledge_bases": 0, "tools": 0})
        self.assertEqual(self._kb().external_id, "")

        # A later chat-ready moment completes the push.
        api = self._patched_api()
        with mock.patch("chat.config.ENABLED", True), mock.patch(
            "chat.api.ChatAPI.as_user", return_value=api
        ):
            counts = backfill_demo_chat_resources(self.project, self.user)
        self.assertEqual(counts, {"agents": 1, "knowledge_bases": 1, "tools": 1})
        self.assertEqual(self._kb().external_id, "kb-backfill")
