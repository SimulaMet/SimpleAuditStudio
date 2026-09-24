"""Tests for workspace (project) CRUD, team membership management, and switching."""
from django.test import Client, TestCase
from rest_framework.test import APIClient

from accounts.models import Project, ProjectMembership, User
from infra.tests.factories import MembershipFactory, ProjectFactory, ScenarioFactory, UserFactory


def _superuser() -> User:
    user = UserFactory()
    user.is_superuser = True
    user.is_staff = True
    user.save()
    return user


class WorkspaceListCreateTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_anonymous_cannot_list(self):
        # DRF SessionAuthentication returns 403 (not 401) for anonymous users.
        resp = self.client.get("/api/projects/")
        self.assertIn(resp.status_code, (401, 403))

    def test_user_without_memberships_sees_empty_and_can_create(self):
        user = UserFactory()
        self.client.force_authenticate(user)
        resp = self.client.get("/api/projects/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

        # Any authenticated user can create their own workspace.
        resp = self.client.post("/api/projects/create/", {"name": "New Team"}, format="json")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["name"], "New Team")
        self.assertTrue(body["is_admin"])
        self.assertTrue(ProjectMembership.objects.filter(project_id=body["id"], user=user, role="admin").exists())

    def test_default_project_visible_to_all_users(self):
        """The 'Default' workspace (slug='default') is always listed for every
        authenticated user, even without an explicit membership."""
        from accounts.models import Project as P

        # Create the Default project and another private one.
        P.objects.create(name="Default", slug="default")
        private = P.objects.create(name="Private", slug="private")

        # User has membership only in 'private'.
        user = UserFactory()
        MembershipFactory(user=user, project=private, role="viewer")
        self.client.force_authenticate(user)

        resp = self.client.get("/api/projects/")
        self.assertEqual(resp.status_code, 200)
        slugs = [item["slug"] for item in resp.json()]
        self.assertIn("default", slugs)
        self.assertIn("private", slugs)

    def test_default_project_accessible_without_membership(self):
        """A user with zero memberships can still access the Default project detail."""
        from accounts.models import Project as P

        default = P.objects.create(name="Default", slug="default")
        user = UserFactory()
        self.client.force_authenticate(user)

        resp = self.client.get(f"/api/projects/{default.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["slug"], "default")

    def test_admin_member_can_create_workspace(self):
        user = UserFactory()
        first = ProjectFactory()
        MembershipFactory(user=user, project=first, role="admin")
        self.client.force_authenticate(user)

        resp = self.client.post("/api/projects/create/", {"name": "Second Team", "description": "d"}, format="json")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["name"], "Second Team")
        self.assertTrue(body["is_admin"])
        self.assertTrue(ProjectMembership.objects.filter(project_id=body["id"], user=user, role="admin").exists())

    def test_superuser_can_create_workspace(self):
        admin = _superuser()
        self.client.force_authenticate(admin)
        resp = self.client.post("/api/projects/create/", {"name": "Ops"}, format="json")
        self.assertEqual(resp.status_code, 201)

    def test_duplicate_name_conflicts_then_slug_suffixed(self):
        user = UserFactory()
        first = ProjectFactory(name="My Team", slug="my-team")
        MembershipFactory(user=user, project=first, role="admin")
        self.client.force_authenticate(user)

        # Exact same name → 409
        resp = self.client.post("/api/projects/create/", {"name": "My Team"}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "workspace_name_conflict")

        # Same slug base, different casing → gets suffixed slug
        resp = self.client.post("/api/projects/create/", {"name": "MY TEAM"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["slug"], "my-team-2")

    def test_empty_name_rejected(self):
        user = UserFactory()
        first = ProjectFactory()
        MembershipFactory(user=user, project=first, role="admin")
        self.client.force_authenticate(user)
        resp = self.client.post("/api/projects/create/", {"name": "   "}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_list_shows_is_admin_per_workspace(self):
        user = UserFactory()
        w_admin = ProjectFactory(name="Alpha")
        w_viewer = ProjectFactory(name="Beta")
        MembershipFactory(user=user, project=w_admin, role="admin")
        MembershipFactory(user=user, project=w_viewer, role="viewer")
        other = ProjectFactory(name="Gamma")  # not a member
        self.client.force_authenticate(user)

        resp = self.client.get("/api/projects/")
        self.assertEqual(resp.status_code, 200)
        items = {item["name"]: item for item in resp.json()}
        self.assertNotIn("Gamma", items)
        self.assertTrue(items["Alpha"]["is_admin"])
        self.assertFalse(items["Beta"]["is_admin"])

    def test_superuser_list_marks_all_admin(self):
        admin = _superuser()
        ProjectFactory(name="One")
        ProjectFactory(name="Two")
        self.client.force_authenticate(admin)
        resp = self.client.get("/api/projects/")
        self.assertEqual(len(resp.json()), 2)
        self.assertTrue(all(item["is_admin"] for item in resp.json()))


class WorkspaceUpdateDeleteTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.project = ProjectFactory(name="Original", slug="original")
        self.admin = UserFactory()
        MembershipFactory(user=self.admin, project=self.project, role="admin")
        self.viewer = UserFactory()
        MembershipFactory(user=self.viewer, project=self.project, role="viewer")
        self.outsider = UserFactory()

    def test_admin_can_rename(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(f"/api/projects/{self.project.id}/", {"name": "Renamed"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Renamed")
        self.assertEqual(self.project.slug, "original")  # slug immutable

    def test_admin_can_update_description(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(f"/api/projects/{self.project.id}/", {"description": "New desc"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.description, "New desc")

    def test_viewer_cannot_update(self):
        self.client.force_authenticate(self.viewer)
        resp = self.client.patch(f"/api/projects/{self.project.id}/", {"name": "Hax"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "workspace_admin_required")

    def test_outsider_cannot_access_detail(self):
        self.client.force_authenticate(self.outsider)
        resp = self.client.get(f"/api/projects/{self.project.id}/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "workspace_access_denied")

    def test_missing_workspace_404(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/projects/99999/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "workspace_not_found")

    def test_admin_can_delete_empty_workspace(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f"/api/projects/{self.project.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Project.objects.filter(pk=self.project.id).exists())

    def test_cannot_delete_workspace_with_content(self):
        ScenarioFactory(project=self.project)
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f"/api/projects/{self.project.id}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "workspace_not_empty")
        self.assertIn("scenarios", resp.json()["error"]["message"])

    def test_viewer_cannot_delete(self):
        self.client.force_authenticate(self.viewer)
        resp = self.client.delete(f"/api/projects/{self.project.id}/")
        self.assertEqual(resp.status_code, 403)


class WorkspaceMemberTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.project = ProjectFactory()
        self.admin = UserFactory(username="ws-admin")
        MembershipFactory(user=self.admin, project=self.project, role="admin")
        self.member = UserFactory(username="member-one")
        MembershipFactory(user=self.member, project=self.project, role="viewer")
        self.superuser = _superuser()

    def test_list_members(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/projects/{self.project.id}/members/")
        self.assertEqual(resp.status_code, 200)
        usernames = {m["username"] for m in resp.json()}
        self.assertEqual(usernames, {"ws-admin", "member-one"})

    def test_viewer_can_list_but_not_add(self):
        self.client.force_authenticate(self.member)
        resp = self.client.get(f"/api/projects/{self.project.id}/members/")
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            f"/api/projects/{self.project.id}/members/add/",
            {"username": "member-one", "role": "auditor"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_admin_adds_member(self):
        new_user = UserFactory(username="newbie")
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f"/api/projects/{self.project.id}/members/add/",
            {"username": "newbie", "role": "auditor"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["role"], "auditor")

    def test_add_unknown_user_404(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f"/api/projects/{self.project.id}/members/add/",
            {"username": "ghost", "role": "viewer"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "user_not_found")

    def test_admin_updates_role(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.put(
            f"/api/projects/{self.project.id}/members/{self.member.id}/",
            {"role": "auditor"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.member.memberships.get(project=self.project).refresh_from_db()
        self.assertEqual(self.member.memberships.get(project=self.project).role, "auditor")

    def test_admin_removes_member(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f"/api/projects/{self.project.id}/members/{self.member.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProjectMembership.objects.filter(project=self.project, user=self.member).exists())

    def test_last_admin_cannot_demote_self(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.put(
            f"/api/projects/{self.project.id}/members/{self.admin.id}/",
            {"role": "auditor"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "last_admin")

    def test_last_admin_cannot_remove_self(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f"/api/projects/{self.project.id}/members/{self.admin.id}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "last_admin")

    def test_second_admin_can_demote(self):
        second = UserFactory(username="second-admin")
        MembershipFactory(user=second, project=self.project, role="admin")
        self.client.force_authenticate(second)
        resp = self.client.put(
            f"/api/projects/{self.project.id}/members/{self.admin.id}/",
            {"role": "viewer"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_cannot_modify_superuser_membership(self):
        MembershipFactory(user=self.superuser, project=self.project, role="admin")
        self.client.force_authenticate(self.admin)
        resp = self.client.put(
            f"/api/projects/{self.project.id}/members/{self.superuser.id}/",
            {"role": "viewer"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "cannot_modify_superuser")

        resp = self.client.delete(f"/api/projects/{self.project.id}/members/{self.superuser.id}/")
        self.assertEqual(resp.status_code, 403)

    def test_remove_non_member_404(self):
        stranger = UserFactory(username="stranger")
        self.client.force_authenticate(self.admin)
        resp = self.client.delete(f"/api/projects/{self.project.id}/members/{stranger.id}/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "member_not_found")


class WorkspaceSwitchTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = UserFactory()
        self.w1 = ProjectFactory(name="Workspace One")
        self.w2 = ProjectFactory(name="Workspace Two")
        MembershipFactory(user=self.user, project=self.w1, role="admin")
        MembershipFactory(user=self.user, project=self.w2, role="viewer")
        self.other = ProjectFactory(name="Not Mine")

    def test_member_can_switch(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/projects/switch/", {"project_id": self.w2.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["workspace"]["id"], self.w2.id)
        self.assertEqual(self.client.session["active_project_id"], self.w2.id)

    def test_non_member_cannot_switch(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/projects/switch/", {"project_id": self.other.id}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "workspace_access_denied")

    def test_switch_requires_valid_id(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/projects/switch/", {"project_id": "abc"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_switch_drives_dashboard_scoping(self):
        """Switching workspaces changes which runs the dashboard shows.

        Uses a real Django session (login) because the switch endpoint writes
        to request.session, which force_authenticate does not provide.
        """
        from infra.tests.factories import AuditRunFactory, ModelEndpointFactory

        self.user.set_password("testpass123")
        self.user.save()
        endpoint = ModelEndpointFactory(project=self.w2)
        run_w2 = AuditRunFactory(
            project=self.w2,
            name="Only-in-W2",
            target_endpoint=endpoint,
            auditor_endpoint=endpoint,
            judge_endpoint=endpoint,
        )
        run_w1 = AuditRunFactory(project=self.w1, name="Only-in-W1")

        client = Client(SERVER_NAME="localhost")
        client.login(username=self.user.username, password="testpass123")
        resp = client.post("/api/projects/switch/", {"project_id": self.w2.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        resp = client.get("/dashboard/")
        self.assertEqual(resp.status_code, 200)
        # The dashboard table shows run IDs (not names); assert on the W2 run's
        # detail link and confirm the W1 run is absent.
        self.assertIn(f"/audits/{run_w2.id}/", resp.content.decode())
        self.assertNotIn(f"/audits/{run_w1.id}/", resp.content.decode())


class LegacyProjectApiTest(TestCase):
    """Old /api/projects/ paths keep working after the workspace rename."""

    def test_legacy_create_as_superuser(self):
        admin = _superuser()
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.post("/api/projects/create/", {"name": "Legacy"}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["name"], "Legacy")

    def test_legacy_list(self):
        user = UserFactory()
        project = ProjectFactory()
        MembershipFactory(user=user, project=project, role="viewer")
        client = APIClient()
        client.force_authenticate(user)
        resp = client.get("/api/projects/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
