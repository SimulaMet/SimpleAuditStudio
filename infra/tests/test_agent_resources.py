"""Tests for the /agents/knowledge/ and /agents/tools/ iframe pages."""
import re
from unittest import mock

from django.test import TestCase

from accounts.models import ProjectMembership
from infra.tests.factories import ProjectFactory, UserFactory
from model_registry.models import KnowledgeBase, Tool


def _member(project, *, is_admin=True):
    user = UserFactory()
    ProjectMembership.objects.create(
        project=project, user=user,
        role=ProjectMembership.Role.ADMIN if is_admin else ProjectMembership.Role.MEMBER,
    )
    return user


class AgentResourcesViewTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self._enabled = mock.patch("chat.config.ENABLED", True)
        self._enabled.start()
        self.addCleanup(self._enabled.stop)

    def _login(self, user):
        self.client.force_login(user)

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get("/agents/knowledge/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_knowledge_page_renders_knowledge_iframe(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/knowledge/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "agents/resources.html")
        self.assertIn("/workspace/knowledge", resp.content.decode())
        self.assertIn("__studio_admin=1", resp.content.decode())
        self.assertIn("Knowledge", resp.content.decode())

    def test_tools_page_renders_tools_iframe(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/tools/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/workspace/tools", resp.content.decode())
        self.assertIn("__studio_admin=1", resp.content.decode())
        self.assertIn("Tools", resp.content.decode())

    def test_knowledge_offers_full_workspace_link(self):
        # The knowledge page offers a button that opens the full workspace in a
        # new tab — the plain section URL (no query string), where the File
        # System Access API directory picker is guaranteed to work. Assert on
        # the button anchor itself, not the whole page: the page also renders
        # the iframe (whose src carries ?embed=admin) and the site footer
        # (which has its own target="_blank" links), so whole-page substring
        # checks would be too broad.
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/knowledge/")
        html = resp.content.decode()
        self.assertIn("Folders, directory sync", html)
        anchors = re.findall(r"<a\s[^>]*>", html, re.DOTALL)
        button = next((a for a in anchors if "Opens the full Knowledge workspace" in a), None)
        self.assertIsNotNone(button, "expected the full-workspace button anchor")
        self.assertIn("target=\"_blank\"", button)
        # Plain section URL with no query string (unlike the iframe src, which
        # carries ?embed=admin&...).
        href = re.search(r'href="([^"]*)"', button).group(1)
        self.assertTrue(href.rstrip("/").endswith("/workspace/knowledge"), href)
        self.assertNotIn("?", href)
        self.assertNotIn("create=1", href)

    def test_tools_does_not_offer_full_workspace_link(self):
        # Tools has no folder-upload flow, so no full-workspace button. The
        # check is on the button anchor, not target="_blank" across the page
        # (the site footer always renders such links).
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/tools/")
        html = resp.content.decode()
        self.assertNotIn("Folders, directory sync", html)
        self.assertIsNone(
            next((a for a in re.findall(r"<a\s[^>]*>", html, re.DOTALL) if "Opens the full" in a), None)
        )

    def test_view_is_login_protected(self):
        resp = self.client.get("/agents/tools/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_sync_updates_seeded_kb_by_name_not_duplicate(self):
        """The seed / chat-ready backfill creates a KB row keyed by name, often
        with a still-empty external_id. When the ⟳ Sync then lists the same KB
        from OWUI, it must UPDATE that row (filling external_id), not CREATE a
        second one whose name already exists — that raised
        ``UNIQUE constraint failed: project_id, name`` (a 500 on /agents/sync/)."""
        from infra.ui import _sync_openwebui_resources

        user = _member(self.project)
        self._login(user)
        seeded = KnowledgeBase.objects.create(
            project=self.project, name="Acme Retail Policy", external_id=""
        )
        api = mock.Mock()
        api.knowledge_bases.return_value = [
            {"id": "owui-kb-1", "name": "Acme Retail Policy", "description": "d"}
        ]
        api.tools.return_value = []

        with mock.patch("chat.api.ChatAPI.as_user", return_value=api):
            kb_count, tool_count = _sync_openwebui_resources(self.project, user)

        self.assertEqual((kb_count, tool_count), (1, 0))
        # Same row updated, not duplicated.
        self.assertEqual(KnowledgeBase.objects.count(), 1)
        self.assertEqual(KnowledgeBase.objects.get(pk=seeded.pk).external_id, "owui-kb-1")

    def test_sync_renames_kb_by_external_id_not_duplicate(self):
        """A KB renamed in OWUI keeps the same id. Sync must update the existing
        row in place (matching on external_id), not CREATE a second row whose
        external_id is already taken — that raised
        ``UNIQUE constraint failed: project_id, external_id`` (a 500)."""
        from infra.ui import _sync_openwebui_resources

        user = _member(self.project)
        seeded = KnowledgeBase.objects.create(
            project=self.project, name="Old Name", external_id="owui-kb-1"
        )
        api = mock.Mock()
        # Same id, new name: a rename.
        api.knowledge_bases.return_value = [
            {"id": "owui-kb-1", "name": "New Name", "description": "d"}
        ]
        api.tools.return_value = []

        with mock.patch("chat.api.ChatAPI.as_user", return_value=api):
            kb_count, tool_count = _sync_openwebui_resources(self.project, user)

        self.assertEqual((kb_count, tool_count), (1, 0))
        # Same row renamed, not duplicated.
        self.assertEqual(KnowledgeBase.objects.count(), 1)
        kb = KnowledgeBase.objects.get(pk=seeded.pk)
        self.assertEqual(kb.name, "New Name")
        self.assertEqual(kb.external_id, "owui-kb-1")

    def test_agent_picker_uses_openwebui_resource_visibility(self):
        """Private OWUI resources do not leak through Studio's local rows."""
        user = _member(self.project)
        self._login(user)
        visible = KnowledgeBase.objects.create(
            project=self.project, name="Public KB", external_id="kb-public"
        )
        KnowledgeBase.objects.create(
            project=self.project, name="Private KB", external_id="kb-private"
        )
        visible_tool = Tool.objects.create(
            project=self.project, name="Public Tool", external_id="tool-public"
        )
        Tool.objects.create(
            project=self.project, name="Private Tool", external_id="tool-private"
        )

        api = mock.Mock()
        api.knowledge_bases.return_value = [
            {"id": "kb-public", "name": "Public KB", "description": ""}
        ]
        api.tools.return_value = [{"id": "tool-public", "name": "Public Tool"}]
        with mock.patch("chat.api.ChatAPI.as_user", return_value=api):
            response = self.client.get("/agents/new/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(visible, response.context["knowledge_bases"])
        self.assertNotIn(
            KnowledgeBase.objects.get(external_id="kb-private"),
            response.context["knowledge_bases"],
        )
        self.assertIn(visible_tool, response.context["tools"])
        self.assertNotIn(
            Tool.objects.get(external_id="tool-private"), response.context["tools"]
        )


class AgentResourcesNavTest(TestCase):
    """The Agents surface in the sidebar while chat is ON (the enabled default
    this whole surface depends on)."""

    def setUp(self):
        self.project = ProjectFactory()
        self.user = _member(self.project)
        self.client.force_login(self.user)
        self._enabled = mock.patch("chat.config.ENABLED", True)
        self._enabled.start()
        self.addCleanup(self._enabled.stop)

    def test_knowledge_and_tools_appear_in_sidebar(self):
        resp = self.client.get("/agents/knowledge/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"agents/knowledge", resp.content)
        self.assertIn(b"agents/tools", resp.content)

    def test_nav_context_has_knowledge_and_tools_as_agents_children(self):
        resp = self.client.get("/agents/")
        items = resp.context["nav_items"]
        # Knowledge Bases and Tools are not top-level items; they sit under Agents.
        top_labels = [i["label"] for i in items]
        self.assertNotIn("Knowledge Bases", top_labels)
        self.assertNotIn("Tools", top_labels)
        agents = next(i for i in items if i["label"] == "Agents")
        knowledge = next(c for c in agents["children"] if c["label"] == "Knowledge Bases")
        self.assertEqual(knowledge["url"], "/agents/knowledge/")
        tools = next(c for c in agents["children"] if c["label"] == "Tools")
        self.assertEqual(tools["url"], "/agents/tools/")


class AgentSurfaceDisabledTest(TestCase):
    """With chat off, the whole Agents surface is hidden: out of the nav, and
    the /agents/ pages show a 'chat not enabled' notice instead of their UI."""

    def setUp(self):
        self.project = ProjectFactory()
        self.user = _member(self.project)
        self.client.force_login(self.user)
        self._disabled = mock.patch("chat.config.ENABLED", False)
        self._disabled.start()
        self.addCleanup(self._disabled.stop)

    def test_agents_not_in_nav_when_chat_off(self):
        resp = self.client.get("/agents/")
        labels = [i["label"] for i in resp.context["nav_items"]]
        self.assertNotIn("Agents", labels)
        self.assertNotIn("Knowledge Bases", labels)
        self.assertNotIn("Tools", labels)

    def test_agents_list_page_shows_notice_and_no_form(self):
        from infra.tests.factories import AgentFactory

        AgentFactory(project=self.project)
        resp = self.client.get("/agents/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Chat is not enabled", html)
        self.assertNotIn("+ New agent", html)
        self.assertNotIn("Test in Chat", html)
        self.assertNotIn("agent_new", html)

    def test_agent_new_page_shows_notice_not_editor(self):
        resp = self.client.get("/agents/new/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Chat is not enabled", html)
        # The editor form (and its Name field) must be absent. (A "<form" string
        # appears in a JS comment in a shared partial, so assert on the editor
        # form's own markup, not the bare tag.)
        self.assertNotIn('<form method="post" class="space-y-6">', html)
        self.assertNotIn('name="name"', html)

    def test_post_new_agent_does_not_create_when_chat_off(self):
        from model_registry.models import Agent
        resp = self.client.post("/agents/new/", {"name": "Ghost", "base_model": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Agent.objects.filter(project=self.project, name="Ghost").count(), 0)

    def test_test_chat_redirects_to_agents_when_chat_off(self):
        from infra.tests.factories import AgentFactory
        agent = AgentFactory(project=self.project)
        resp = self.client.get(f"/agents/{agent.id}/test-chat/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/agents/")

    def test_new_experiment_picker_has_no_agents_group_when_chat_off(self):
        resp = self.client.get("/experiments/new/")
        self.assertEqual(resp.status_code, 200)
        # agent_models only lists *synced* agents; with chat off none exist, so
        # the picker must not offer an "Agents" group at all.
        self.assertEqual(list(resp.context["agent_models"]), [])
        self.assertNotIn(b">Agents<", resp.content)

    def test_new_experiment_picker_hides_agents_group_for_synced_agent_when_chat_off(self):
        # Reproduces the seeded live-data leak: an agent that was previously
        # synced to Open WebUI (non-empty external_id) still exists locally
        # after chat is turned off. The picker must not surface it as an
        # "Agents" group — agents need Open WebUI to be testable.
        from infra.tests.factories import (
            AgentFactory,
            ModelConnectionFactory,
            RegisteredModelFactory,
        )

        conn = ModelConnectionFactory(project=self.project)
        RegisteredModelFactory(connection=conn, model_id="studio.agent-1")
        AgentFactory(project=self.project, external_id="studio.agent-1")

        resp = self.client.get("/experiments/new/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context["agent_models"]), [])
        self.assertNotIn(b">Agents<", resp.content)


class AgentResourcesDisabledTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = _member(self.project)
        self.client.force_login(self.user)

    def test_chat_disabled_shows_notice_not_iframe(self):
        with mock.patch("chat.config.ENABLED", False):
            resp = self.client.get("/agents/knowledge/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"<iframe", resp.content)
        self.assertIn(b"not enabled", resp.content)
