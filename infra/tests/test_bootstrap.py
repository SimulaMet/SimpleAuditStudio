import os
from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import Project, ProjectMembership

User = get_user_model()


@contextmanager
def _safe_env():
    """Provide a safe, non-default config so the startup check passes on both
    local SQLite and real Postgres. The bootstrap command still enforces these
    checks; we simply supply valid values for the duration of the test."""
    values = {
        "DJANGO_SECRET_KEY": "test-secret-key-not-change-me",
        "POSTGRES_PASSWORD": "testpass123",
        "MINIO_ACCESS_KEY": "test-minio-access",
        "MINIO_SECRET_KEY": "test-minio-secret",
    }
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class BootstrapTests(TestCase):
    def test_bootstrap_is_idempotent(self):
        with _safe_env():
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
        with _safe_env():
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
