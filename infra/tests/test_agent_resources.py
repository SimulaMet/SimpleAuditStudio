"""Tests for the /agents/knowledge/ and /agents/tools/ iframe pages."""
from unittest import mock

from django.test import TestCase

from accounts.models import ProjectMembership
from infra.tests.factories import ProjectFactory, UserFactory


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

    def test_view_is_login_protected(self):
        resp = self.client.get("/agents/tools/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])


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
        # Knowledge and Tools are not top-level items; they sit under Agents.
        top_labels = [i["label"] for i in items]
        self.assertNotIn("Knowledge", top_labels)
        self.assertNotIn("Tools", top_labels)
        agents = next(i for i in items if i["label"] == "Agents")
        knowledge = next(c for c in agents["children"] if c["label"] == "Knowledge")
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
