"""UI tests for the workspace switcher and /workspaces/ page."""
from django.test import Client, TestCase

from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory


class WorkspacesPageTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("testpass123")
        self.user.save()
        self.w_admin = ProjectFactory(name="Alpha Team")
        self.w_viewer = ProjectFactory(name="Beta Team")
        MembershipFactory(user=self.user, project=self.w_admin, role="admin")
        MembershipFactory(user=self.user, project=self.w_viewer, role="viewer")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password="testpass123")

    def test_anonymous_redirected_to_login(self):
        anon = Client(SERVER_NAME="localhost")
        resp = anon.get("/workspaces/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_page_lists_all_member_workspaces(self):
        resp = self.client.get("/workspaces/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Alpha Team", content)
        self.assertIn("Beta Team", content)

    def test_sidebar_switcher_shows_current_workspace(self):
        # Default active project = first membership (Alpha Team by name order)
        resp = self.client.get("/dashboard/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('id="workspace-switcher"', content)
        self.assertIn("Alpha Team", content)

    def test_manage_panel_visible_for_admin(self):
        resp = self.client.get(f"/workspaces/?manage={self.w_admin.id}")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Manage — Alpha Team", content)
        self.assertIn("Add member", content)
        self.assertIn("Danger zone", content)
        self.assertIn("Workspace details", content)

    def test_manage_panel_read_only_for_viewer(self):
        resp = self.client.get(f"/workspaces/?manage={self.w_viewer.id}")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("Manage — Beta Team", content)
        self.assertNotIn("Add member", content)
        self.assertNotIn("Danger zone", content)
        self.assertNotIn("Workspace details", content)

    def test_manage_other_users_workspace_hidden(self):
        stranger_ws = ProjectFactory(name="Gamma Team")
        resp = self.client.get(f"/workspaces/?manage={stranger_ws.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Manage — Gamma Team", resp.content.decode())

    def test_new_workspace_button_for_admin(self):
        resp = self.client.get("/workspaces/")
        self.assertIn("New workspace", resp.content.decode())

    def test_new_workspace_button_for_viewer_only_user(self):
        """A user who is only a viewer (e.g. WorkOS magic-auth signup) can still create."""
        viewer = UserFactory()
        viewer.set_password("testpass123")
        viewer.save()
        MembershipFactory(user=viewer, project=self.w_viewer, role="viewer")
        c = Client(SERVER_NAME="localhost")
        c.login(username=viewer.username, password="testpass123")
        resp = c.get("/workspaces/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("New workspace", resp.content.decode())

    def test_csrf_cookie_set_for_authenticated_user(self):
        """The csrftoken cookie must be present so fetch POSTs work in iframes."""
        resp = self.client.get("/workspaces/")
        self.assertIn("csrftoken", resp.cookies)

    def test_no_workspaces_state(self):
        lonely = UserFactory()
        lonely.set_password("testpass123")
        lonely.save()
        c = Client(SERVER_NAME="localhost")
        c.login(username=lonely.username, password="testpass123")
        resp = c.get("/workspaces/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("You don't belong to any workspace yet.", content)
        # A user with zero memberships must still see the create button.
        self.assertIn("New workspace", content)
