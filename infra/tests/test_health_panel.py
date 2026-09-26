"""Tests for the admin Health panel: probes, API auth gating, and page render."""
import time
from unittest import mock

from django.test import Client, TestCase
from rest_framework.test import APIClient

from accounts.models import ProjectMembership
from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)


def _make_admin():
    user = UserFactory()
    project = ProjectFactory()
    MembershipFactory(user=user, project=project, role=ProjectMembership.Role.ADMIN)
    return user, project


def _make_nonadmin():
    user = UserFactory()
    project = ProjectFactory()
    MembershipFactory(user=user, project=project, role=ProjectMembership.Role.VIEWER)
    return user, project


class HealthProbeTest(TestCase):
    """Each probe must return a safe dict; one failure must not break others."""

    def test_collect_health_returns_all_sections(self):
        from infra.health import collect_health

        data = collect_health()
        self.assertIn("overall", data)
        self.assertIn("components", data)
        self.assertIn("resources", data)
        for name in ("web", "postgres", "hatchet", "worker", "minio", "engine", "model_endpoints"):
            self.assertIn(name, data["components"])
        for name in ("memory", "disk", "cpu", "queue"):
            self.assertIn(name, data["resources"])

    def test_postgres_down_does_not_break_other_probes(self):
        from infra import health

        with mock.patch.object(health, "_postgres_probe", side_effect=RuntimeError("db down")):
            data = health.collect_health()
        self.assertEqual(data["components"]["postgres"]["status"], "down")
        # Web probe still reports up independently.
        self.assertEqual(data["components"]["web"]["status"], "up")

    def test_engine_probe_reports_metadata(self):
        from infra.health import _engine_probe

        result = _engine_probe()
        # In the test env the engine may or may not be installed; either way it
        # must return a well-formed status.
        self.assertIn(result["status"], ("up", "down"))

    def test_model_endpoints_probe_lists_enabled_endpoints(self):
        from infra.health import _model_endpoints_probe

        _user, project = _make_admin()
        conn = ModelConnectionFactory(project=project, base_url="http://127.0.0.1:1/v1", enabled=True)
        model = RegisteredModelFactory(connection=conn, project=project, enabled=True)
        result = _model_endpoints_probe()
        groups = result.get("groups", [])
        self.assertTrue(any(g["name"] == conn.name for g in groups))
        group = next(g for g in groups if g["name"] == conn.name)
        model_ids = [m["id"] for m in group["models"]]
        self.assertIn(model.id, model_ids)

    def test_queue_throughput_counts_runs(self):
        from audits.models import AuditRun
        from infra.health import _queue_throughput_probe
        from infra.tests.factories import (
            AuditRunFactory,
            ScenarioSetFactory,
            ScenarioSetVersionFactory,
        )

        _user, project = _make_admin()
        sset = ScenarioSetFactory(project=project)
        version = ScenarioSetVersionFactory(scenario_set=sset, version=1)
        model = RegisteredModelFactory(project=project)
        AuditRunFactory(
            project=project, scenario_set_version=version,
            target_model=model, auditor_model=model, judge_model=model,
            status=AuditRun.Status.COMPLETED,
        )
        result = _queue_throughput_probe()
        self.assertGreaterEqual(result["runs"]["completed"], 1)

    def test_minio_probe_fails_fast_when_unreachable(self):
        """A health probe must not hang; an unreachable MinIO should fail in <5s."""
        from django.test import override_settings

        from infra import health

        with override_settings(MINIO_ENDPOINT="http://127.0.0.1:1", MINIO_ACCESS_KEY="test", MINIO_SECRET_KEY="test", MINIO_BUCKET="b"):
            start = time.monotonic()
            # Call through _probe() exactly as collect_health() does, so the
            # raised ClientError is normalized to a down component.
            result = health._probe(health._minio_probe)
            elapsed = time.monotonic() - start
        self.assertEqual(result["status"], "down")
        self.assertLess(elapsed, 5.0, f"MinIO probe took {elapsed:.1f}s (should fail fast)")


class HealthApiAuthTest(TestCase):
    """The /api/health/ endpoint must be admin-only."""

    def setUp(self):
        self.client = APIClient()

    def test_anonymous_gets_401_or_403(self):
        resp = self.client.get("/api/health/")
        self.assertIn(resp.status_code, (401, 403))

    def test_non_admin_gets_403(self):
        user, _ = _make_nonadmin()
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_gets_200_with_payload(self):
        user, _ = _make_admin()
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("components", payload)
        self.assertIn("resources", payload)
        self.assertIn("overall", payload)

    def test_superuser_gets_200(self):
        user = UserFactory()
        user.is_superuser = True
        user.save()
        self.client.force_authenticate(user=user)
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)


class HealthPageRenderTest(TestCase):
    """The /health/ page renders for admins and is forbidden for non-admins."""

    def setUp(self):
        self.client = Client()

    def test_anonymous_does_not_500(self):
        """AnonymousUser has no .id; the view must redirect to login, not crash."""
        resp = self.client.get("/health/")
        # LoginRequiredMixin redirects (302) to the login page.
        self.assertIn(resp.status_code, (301, 302))

    def test_page_forbidden_for_non_admin(self):
        user, _ = _make_nonadmin()
        self.client.force_login(user)
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 403)

    def test_page_renders_for_admin(self):
        user, _ = _make_admin()
        self.client.force_login(user)
        resp = self.client.get("/health/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("System Health", content)
        self.assertIn("Components", content)

    def test_sidebar_shows_health_link_only_for_admin(self):
        admin_user, _ = _make_admin()
        self.client.force_login(admin_user)
        admin_page = self.client.get("/dashboard/").content.decode()
        self.assertIn('href="/health/"', admin_page)

        nonadmin_user, _ = _make_nonadmin()
        self.client.force_login(nonadmin_user)
        nonadmin_page = self.client.get("/dashboard/").content.decode()
        self.assertNotIn('href="/health/"', nonadmin_page)
