import os

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


class DevServerBootstrapTests(TestCase):
    """`dev_server` bootstraps on start, so a stale local admin becomes a superuser again."""

    def test_prepare_database_promotes_existing_admin(self):
        import io
        from unittest import mock

        from infra.management.commands.dev_server import Command

        User.objects.create_user(username="studio", password="old", is_staff=True, is_superuser=False)
        env = {"BOOTSTRAP_USERNAME": "studio", "BOOTSTRAP_PASSWORD": "localdevpass123"}
        with safe_env(), mock.patch.dict(os.environ, env), mock.patch("infra.management.commands.dev_server.call_command",
                                                                    side_effect=_skip_migrate):
            Command(stdout=io.StringIO()).prepare_database()
        admin = User.objects.get(username="studio")
        self.assertTrue(admin.is_superuser)
        self.assertTrue(Project.objects.filter(memberships__user=admin).exists())

    def test_prepare_database_without_password_only_migrates(self):
        import io
        from unittest import mock

        from infra.management.commands.dev_server import Command

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BOOTSTRAP_PASSWORD": ""}), \
             mock.patch("infra.management.commands.dev_server.call_command") as cmd:
            Command(stdout=out).prepare_database()
        self.assertEqual([c.args[0] for c in cmd.call_args_list], ["migrate"])
        self.assertIn("skipping admin bootstrap", out.getvalue())


def _skip_migrate(name, *args, **kwargs):
    """The test database is already migrated; run every other command for real."""
    if name != "migrate":
        call_command(name, *args, **kwargs)
