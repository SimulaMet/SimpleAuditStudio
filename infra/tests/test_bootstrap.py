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
                    username="admin",
                    email="admin@example.local",
                    password="admin-pass-123",
                    project_name="Default",
                )

        project = Project.objects.get(slug="default")
        memberships = ProjectMembership.objects.filter(project=project)
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships.first().role, ProjectMembership.Role.ADMIN)

    def test_bootstrap_admin_is_not_superuser(self):
        """The bootstrap admin must be a normal account (is_staff only), not a
        Django superuser. Superuser status bypasses all workspace membership
        checks, making the admin appear as owner of every workspace."""
        with _safe_env():
            call_command(
                "bootstrap_platform",
                username="admin",
                email="admin@example.local",
                    password="admin-pass-123",
                project_name="Default",
            )

        user = User.objects.get(username="admin")
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_staff)


    def test_bootstrap_demotes_legacy_superuser(self):
        """If a pre-existing admin was created as superuser by an older
        bootstrap version, re-running bootstrap must demote it so workspace
        isolation is restored on the next container start."""
        legacy = User.objects.create_user(
            username="admin", password="old-pass", is_superuser=True
        )
        self.assertTrue(legacy.is_superuser)

        with _safe_env():
            call_command(
                "bootstrap_platform",
                username="admin",
                email="admin@example.local",
                    password="admin-pass-123",
                project_name="Default",
            )

        legacy.refresh_from_db()
        self.assertFalse(legacy.is_superuser)
        self.assertTrue(legacy.is_staff)