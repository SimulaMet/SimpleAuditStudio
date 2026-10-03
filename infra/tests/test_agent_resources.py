"""Tests for the /agents/resources/ restricted-iframe resource manager."""
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
        # The resource manager only renders the iframe when chat is enabled.
        self._enabled = mock.patch("chat.config.ENABLED", True)
        self._enabled.start()
        self.addCleanup(self._enabled.stop)

    def _login(self, user):
        self.client.force_login(user)

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_member_gets_the_page_with_the_knowledge_iframe(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "agents/resources.html")
        # Default section is knowledge; the iframe points at the workspace
        # knowledge page through the proxy with the admin marker.
        self.assertIn("/workspace/knowledge", resp.content.decode())
        self.assertIn("__studio_admin=1", resp.content.decode())

    def test_section_param_selects_the_tools_iframe(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/resources/?section=tools")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/workspace/tools", resp.content.decode())
        self.assertIn("__studio_admin=1", resp.content.decode())

    def test_unknown_section_falls_back_to_knowledge(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/resources/?section=bogus")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/workspace/knowledge", resp.content.decode())

    def test_view_is_login_protected_like_agents(self):
        # Anonymous users are bounced to login (LoginRequiredMixin), the same
        # protection AgentsView has. Membership scoping is handled by
        # ProjectMiddleware, not this view.
        resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])


class AgentResourcesNavTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = _member(self.project)
        self.client.force_login(self.user)

    def test_resources_appears_in_nav_context(self):
        resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 200)
        # The sidebar renders a Resources link to the resource manager.
        self.assertIn(b"agents/resources", resp.content)
        self.assertIn(b"Resources", resp.content)

    def test_nav_context_has_resources_child_under_agents(self):
        from infra.context_processors import nav
        resp = self.client.get("/agents/")
        items = resp.context["nav_items"]
        agents = next(i for i in items if i["label"] == "Agents")
        child = next(c for c in agents["children"] if c["label"] == "Resources")
        self.assertEqual(child["url"], "/agents/resources/")


class AgentResourcesDisabledTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = _member(self.project)
        self.client.force_login(self.user)

    def test_chat_disabled_shows_notice_not_iframe(self):
        with mock.patch("chat.config.ENABLED", False):
            resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"<iframe", resp.content)
        self.assertIn(b"not enabled", resp.content)
