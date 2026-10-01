"""Every user-creation path must land the new user in the 'default' workspace.

A user with no project membership makes ``request.project`` resolve to ``None``
and 500s every project-scoped view. These tests pin the invariant that each
door into the system (admin add-user, self-registration, demo signup) grants a
viewer membership in the shared Default workspace, and that the middleware
backstop keeps even a membership-less user from crashing.
"""
from django.test import Client, TestCase

from accounts.models import Project, ProjectMembership, User
from infra.tests.factories import ProjectFactory
from infra.tests.utils import login, post_json, superuser


def _default_project():
    return Project.objects.create(name="Default", slug="default")


class AdminCreateUserAssignmentTest(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")
        self.admin = superuser()
        login(self.client, self.admin)

    def test_admin_added_user_gets_default_viewer(self):
        _default_project()
        resp = post_json(
            self.client,
            "/api/admin/users/",
            {"username": "newbie", "email": "newbie@test.com", "password": "Str0ng-pass-123"},
        )
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(username="newbie")
        default = Project.objects.get(slug="default")
        self.assertTrue(
            ProjectMembership.objects.filter(
                project=default, user=user, role=ProjectMembership.Role.VIEWER
            ).exists()
        )

    def test_admin_added_user_without_default_project_is_safe(self):
        # No 'default' project exists (e.g. pre-bootstrap): user is created,
        # just without a membership — no crash.
        resp = post_json(
            self.client,
            "/api/admin/users/",
            {"username": "orphan", "password": "Str0ng-pass-123"},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ProjectMembership.objects.filter(user__username="orphan").count(), 0)


class RegisterApiAssignmentTest(TestCase):
    def test_register_grants_default_viewer(self):
        _default_project()
        resp = self.client.post(
            "/api/auth/register/",
            {"username": "carol", "email": "carol@example.com", "password": "another-strong-pass"},
        )
        self.assertEqual(resp.status_code, 201)
        user = User.objects.get(username="carol")
        default = Project.objects.get(slug="default")
        self.assertTrue(
            ProjectMembership.objects.filter(
                project=default, user=user, role=ProjectMembership.Role.VIEWER
            ).exists()
        )


class DemoSignupAssignmentTest(TestCase):
    def test_demo_register_grants_default_viewer(self):
        _default_project()
        resp = self.client.post(
            "/register/",
            {"username": "demo-user", "password": "Str0ng-pass-123", "email": "demo@test.com"},
        )
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username="demo-user")
        default = Project.objects.get(slug="default")
        self.assertTrue(
            ProjectMembership.objects.filter(
                project=default, user=user, role=ProjectMembership.Role.VIEWER
            ).exists()
        )


class ProjectMiddlewareBackstopTest(TestCase):
    """A user with no membership must still resolve a project (no 500)."""

    def test_membershipless_user_falls_back_to_default(self):
        default = _default_project()
        # A non-default project exists too, to prove the fallback is by slug.
        ProjectFactory(name="Other", slug="other")
        user = User.objects.create_user(username="orphan", password="orphan-pass-1")

        client = Client(SERVER_NAME="localhost")
        client.force_login(user)
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.wsgi_request.project, default)

    def test_membershipless_user_without_default_project_gets_none(self):
        # No 'default' project at all: request.project stays None (the views
        # must tolerate that), but the request itself must not 500.
        ProjectFactory(name="Other", slug="other")
        user = User.objects.create_user(username="orphan2", password="orphan-pass-2")

        client = Client(SERVER_NAME="localhost")
        client.force_login(user)
        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
