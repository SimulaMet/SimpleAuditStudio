"""Tests for the idempotent first-run seed command (seed_platform)."""
import os
from contextlib import contextmanager

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from model_registry.models import ModelConnection, ModelEndpoint, RegisteredModel
from scenarios.models import Scenario, ScenarioSet, ScenarioSetVersion


@contextmanager
def _safe_env():
    """Provide a safe, non-default config so the startup check passes on both
    local SQLite and real Postgres (same pattern as test_bootstrap.py)."""
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


class SeedPlatformTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="studio", email="admin@example.local", password="pass12345"
        )
        self.project = Project.objects.create(name="Default", slug="default")
        ProjectMembership.objects.create(
            project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN
        )

    def _run_seed(self, **extra):
        with _safe_env():
            call_command("seed_platform", project=self.project.id, **extra)

    def test_seed_creates_scenario_sets_and_versions(self):
        self._run_seed()

        sets = ScenarioSet.objects.filter(project=self.project)
        self.assertGreaterEqual(sets.count(), 4)

        for pack in ("safety", "rag", "health", "system_prompt"):
            set_obj = ScenarioSet.objects.filter(
                project=self.project, name=f"SimpleAudit: {pack}"
            ).first()
            self.assertIsNotNone(set_obj, f"missing set for pack {pack}")
            versions = ScenarioSetVersion.objects.filter(scenario_set=set_obj)
            self.assertEqual(versions.count(), 1)
            version = versions.first()
            self.assertEqual(version.version, 1)
            self.assertGreater(version.scenario_count, 0)
            # Every version item points at a revision of a scenario in the project.
            for item in version.items.all():
                self.assertEqual(item.scenario.project_id, self.project.id)
                self.assertEqual(item.revision.scenario_id, item.scenario_id)

    def test_seed_is_idempotent(self):
        self._run_seed()
        sets_before = ScenarioSet.objects.filter(project=self.project).count()
        scenarios_before = Scenario.objects.filter(project=self.project).count()
        versions_before = ScenarioSetVersion.objects.filter(
            scenario_set__project=self.project
        ).count()
        models_before = RegisteredModel.objects.filter(project=self.project).count()

        self._run_seed()

        self.assertEqual(ScenarioSet.objects.filter(project=self.project).count(), sets_before)
        self.assertEqual(Scenario.objects.filter(project=self.project).count(), scenarios_before)
        self.assertEqual(
            ScenarioSetVersion.objects.filter(scenario_set__project=self.project).count(),
            versions_before,
        )
        self.assertEqual(RegisteredModel.objects.filter(project=self.project).count(), models_before)

    def test_seed_creates_default_model_connections(self):
        self._run_seed()

        conn = ModelConnection.objects.filter(project=self.project, name="OpenAI").first()
        self.assertIsNotNone(conn)
        self.assertEqual(conn.provider, "openai")
        self.assertEqual(conn.base_url, "https://api.openai.com/v1")

        model_ids = set(
            RegisteredModel.objects.filter(project=self.project, connection=conn).values_list(
                "model_id", flat=True
            )
        )
        self.assertIn("gpt-4o", model_ids)
        self.assertIn("gpt-4o-mini", model_ids)

        # Legacy ModelEndpoint rows exist for AuditRun FK compatibility.
        self.assertGreaterEqual(
            ModelEndpoint.objects.filter(project=self.project).count(), 2
        )

    def test_seed_skips_packs_flag(self):
        self._run_seed(skip_packs=True)
        self.assertEqual(ScenarioSet.objects.filter(project=self.project).count(), 0)
        # Models still seeded.
        self.assertGreaterEqual(RegisteredModel.objects.filter(project=self.project).count(), 1)

    def test_seed_skips_models_flag(self):
        self._run_seed(skip_models=True)
        self.assertEqual(ModelConnection.objects.filter(project=self.project).count(), 0)
        # Packs still seeded.
        self.assertGreaterEqual(ScenarioSet.objects.filter(project=self.project).count(), 1)

    def test_seed_recovers_from_interrupted_import(self):
        """A set created by an interrupted run (no published version) is
        re-imported cleanly instead of being skipped forever."""
        from scenarios.models import ScenarioRevision

        set_obj = ScenarioSet.objects.create(
            project=self.project, name="SimpleAudit: safety", created_by=self.user
        )
        # Orphan scenario from the interrupted run, no revision published.
        Scenario.objects.create(
            project=self.project,
            key="simpleaudit_safety_000",
            title="Harmful Instructions",
            created_by=self.user,
        )

        self._run_seed()

        # The interrupted set is dropped and re-created (new PK).
        set_obj = ScenarioSet.objects.get(project=self.project, name="SimpleAudit: safety")
        versions = ScenarioSetVersion.objects.filter(scenario_set=set_obj)
        self.assertEqual(versions.count(), 1)
        self.assertGreater(versions.first().scenario_count, 0)
        # No duplicate scenarios for the same key.
        self.assertEqual(
            Scenario.objects.filter(project=self.project, key="simpleaudit_safety_000").count(),
            1,
        )

    def test_seed_fails_cleanly_without_project(self):
        from django.core.management.base import CommandError

        with _safe_env():
            with self.assertRaises(CommandError):
                call_command("seed_platform", project=99999)
