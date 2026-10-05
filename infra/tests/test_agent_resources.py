"""Tests for the /agents/knowledge/ and /agents/tools/ iframe pages."""
import re
from unittest import mock

from django.test import TestCase

from accounts.models import ProjectMembership
from infra.tests.factories import ProjectFactory, UserFactory
from model_registry.models import KnowledgeBase


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


class AgentResourcesNavTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = _member(self.project)
        self.client.force_login(self.user)

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
