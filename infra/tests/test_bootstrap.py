from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Project, ProjectMembership
from infra.tests.utils import safe_env

User = get_user_model()


class BootstrapTests(TestCase):
    def test_bootstrap_is_idempotent(self):
        with safe_env():
            for _ in range(2):
                call_command(
                    "bootstrap_platform",
                    username="studio",
                    email="admin@example.local",
                    password="admin-pass-123",
                    project_name="Default",
                )

        project = Project.objects.get(slug="default")
        memberships = ProjectMembership.objects.filter(project=project)
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships.first().role, ProjectMembership.Role.ADMIN)

    def test_bootstrap_admin_is_superuser(self):
        """The bootstrap admin is a platform manager (Django superuser) so it
        can access the Admin Dashboard and manage all workspaces/users."""
        with safe_env():
            call_command(
                "bootstrap_platform",
                username="studio",
                email="admin@example.local",
                password="admin123456789",
                project_name="Default",
            )

        user = User.objects.get(username="studio")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
