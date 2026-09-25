"""API-level end-to-end test: full audit lifecycle through the REST API.

Exercises: auth → create scenario → publish set version → create endpoints →
submit audit run → verify frozen manifest → poll events → compare runs.

This is the "another developer can run this" integration test that validates
the entire user journey without requiring a live worker or Docker stack.
"""
from rest_framework.test import APITestCase

from audits.events import ScenarioResult
from audits.models import AuditRun
from accounts.models import Project, ProjectMembership, User
from scenarios.models import (
    ScenarioSetVersionItem,
)


class APIE2ETest(APITestCase):
    """Full user journey via the REST API (no live worker)."""

    def setUp(self):
        self.user = User.objects.create_user(username="e2euser", password="testpass123")
        self.client.force_authenticate(user=self.user)
        self.project = Project.objects.create(name="E2E", slug="e2e")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.pid = self.project.id

    def _auth(self):
        """Get a token via the auth endpoint."""
        resp = self.client.post("/api/auth/token/", {"username": "e2euser", "password": "testpass123"}, format="json")
        assert resp.status_code == 200, f"Auth failed: {resp.status_code} {resp.content}"
        return resp.json()["token"]

    def test_full_audit_lifecycle(self):
        # 1. Auth
        token = self._auth()
        self.assertTrue(token)

        # 2. Create scenario
        resp = self.client.post(f"/api/projects/{self.pid}/scenarios/create/", {
            "key": "e2e-test-scenario", "title": "E2E Test",
            "description": "Test scenario", "expected_behavior": ["Be safe"], "test_prompt": "Hello",
        }, format="json")
        assert resp.status_code == 201, f"Scenario create failed: {resp.status_code} {resp.content}"
        scenario_id = resp.json()["id"]

        # 3. Create scenario set
        resp = self.client.post(f"/api/projects/{self.pid}/scenario-sets/create/", {"name": "E2E Set"}, format="json")
        assert resp.status_code == 201, f"Set create failed: {resp.status_code} {resp.content}"
        set_id = resp.json()["id"]

        # 4. Publish version (creates revisions for listed scenarios)
        resp = self.client.post(f"/api/projects/{self.pid}/scenario-sets/{set_id}/publish/", {
            "scenario_ids": [scenario_id],
        }, format="json")
        assert resp.status_code == 201, f"Publish version failed: {resp.status_code} {resp.content}"
        version_id = resp.json()["id"]

        # 6. Create model endpoints
        ep_data = {"display_name": "Test Model", "provider": "openai", "base_url": "http://mock/v1", "model_id": "m", "secret_reference": ""}
        resp = self.client.post(f"/api/projects/{self.pid}/model-endpoints/create/", ep_data, format="json")
        assert resp.status_code == 201, f"Endpoint create failed: {resp.status_code} {resp.content}"
        target_id = resp.json()["id"]

        resp = self.client.post(f"/api/projects/{self.pid}/model-endpoints/create/", {**ep_data, "display_name": "Judge Model"}, format="json")
        judge_id = resp.json()["id"]

        # 7. Submit audit run
        resp = self.client.post(f"/api/projects/{self.pid}/audit-runs/create/", {
            "name": "E2E Lifecycle Run",
            "scenario_set_version_id": version_id,
            "target_endpoint_id": target_id,
            "auditor_endpoint_id": target_id,
            "judge_endpoint_id": judge_id,
        }, format="json")
        assert resp.status_code == 201, f"Run create failed: {resp.status_code} {resp.content}"
        run_data = resp.json()
        run_id = run_data["id"]

        # 8. Verify frozen manifest
        self.assertEqual(run_data["status"], "queued")
        self.assertEqual(run_data["total_scenarios"], 1)
        self.assertIsNotNone(run_data.get("simpleaudit_version"))
        self.assertIsNotNone(run_data.get("git_commit"))
        self.assertIn("target_config_snapshot", run_data)
        self.assertIn("judge_config_snapshot", run_data)

        # 9. Get run detail
        resp = self.client.get(f"/api/projects/{self.pid}/audit-runs/{run_id}/")
        assert resp.status_code == 200
        detail = resp.json()
        self.assertEqual(detail["name"], "E2E Lifecycle Run")
        self.assertEqual(detail["scenario_set_version_number"], 1)

        # 10. Poll events (may be empty if Hatchet unavailable in test env)
        resp = self.client.get(f"/api/projects/{self.pid}/audit-runs/{run_id}/events/poll/?after_id=0")
        assert resp.status_code == 200
        events = resp.json()
        self.assertIsInstance(events, list)

        # 11. Simulate completion (worker would do this)
        from audits.events import append_event
        item = ScenarioSetVersionItem.objects.get(version_id=version_id)
        append_event(run_id, str(item.id), "scenario_attempted", {"attempt": 1})
        ScenarioResult.objects.create(
            run_id=str(run_id), version_item_id=str(item.id),
            status="completed", attempts=1, result={"severity": "pass", "summary": "OK"},
        )
        append_event(run_id, str(item.id), "scenario_completed", {"attempt": 1, "severity": "pass"})
        run = AuditRun.objects.get(id=run_id)
        run.completed_scenarios = 1
        run.successful_scenarios = 1
        run.status = AuditRun.Status.COMPLETED
        run.save()
        append_event(run_id, "_run", "run_completed", {"scenarios": 1})

        # 12. Verify results endpoint
        resp = self.client.get(f"/api/projects/{self.pid}/audit-runs/{run_id}/results/")
        assert resp.status_code == 200
        results = resp.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "completed")
        self.assertEqual(results[0]["result"]["severity"], "pass")

        # 13. Compare (single run — should work but show 0 intersection issues)
        # Create a second run for comparison
        resp = self.client.post(f"/api/projects/{self.pid}/audit-runs/create/", {
            "name": "E2E Compare Run",
            "scenario_set_version_id": version_id,
            "target_endpoint_id": target_id,
            "auditor_endpoint_id": target_id,
            "judge_endpoint_id": judge_id,
        }, format="json")
        assert resp.status_code == 201
        run2_id = resp.json()["id"]
        # Complete it too
        item2 = ScenarioSetVersionItem.objects.get(version_id=version_id)
        ScenarioResult.objects.create(
            run_id=str(run2_id), version_item_id=str(item2.id),
            status="completed", attempts=1, result={"severity": "high", "summary": "Issue found"},
        )
        r2 = AuditRun.objects.get(id=run2_id)
        r2.completed_scenarios = 1
        r2.successful_scenarios = 1
        r2.status = AuditRun.Status.COMPLETED
        r2.save()

        # 14. Compare both runs
        resp = self.client.get(f"/api/projects/{self.pid}/audit-runs/compare/?run_ids={run_id},{run2_id}")
        assert resp.status_code == 200, f"Compare failed: {resp.status_code} {resp.content}"
        cmp = resp.json()
        self.assertTrue(cmp["compatible"])
        self.assertEqual(cmp["intersection_count"], 1)
        self.assertEqual(len(cmp["results"]), 1)
        # First run had "pass", second had "high"
        row = cmp["results"][0]
        self.assertEqual(row["runs"][str(run_id)]["severity"], "pass")
        self.assertEqual(row["runs"][str(run2_id)]["severity"], "high")

    def test_compare_requires_min_two_runs(self):
        resp = self.client.get(f"/api/projects/{self.pid}/audit-runs/compare/?run_ids=1")
        self.assertEqual(resp.status_code, 400)

    def test_compare_rejects_unknown_run(self):
        resp = self.client.get(f"/api/projects/{self.pid}/audit-runs/compare/?run_ids=1,99999")
        self.assertEqual(resp.status_code, 409)

    def _make_queued_run(self, name="Cancel Test Run"):
        """Create a minimal queued run for cancel testing."""
        # Scenario + set + version
        resp = self.client.post(f"/api/projects/{self.pid}/scenarios/create/", {
            "key": f"cancel-{name.lower().replace(' ', '-')}", "title": name,
            "description": "d", "expected_behavior": ["x"], "test_prompt": "p",
        }, format="json")
        sid = resp.json()["id"]
        resp = self.client.post(f"/api/projects/{self.pid}/scenario-sets/create/", {"name": f"Set {name}"}, format="json")
        set_id = resp.json()["id"]
        resp = self.client.post(f"/api/projects/{self.pid}/scenario-sets/{set_id}/publish/", {"scenario_ids": [sid]}, format="json")
        ver_id = resp.json()["id"]
        # Endpoints
        ep = {"display_name": "T", "provider": "openai", "base_url": "http://m/v1", "model_id": "m", "secret_reference": ""}
        r1 = self.client.post(f"/api/projects/{self.pid}/model-endpoints/create/", ep, format="json").json()
        r2 = self.client.post(f"/api/projects/{self.pid}/model-endpoints/create/", {**ep, "display_name": "J"}, format="json").json()
        # Run
        resp = self.client.post(f"/api/projects/{self.pid}/audit-runs/create/", {
            "name": name, "scenario_set_version_id": ver_id,
            "target_endpoint_id": r1["id"], "auditor_endpoint_id": r1["id"], "judge_endpoint_id": r2["id"],
        }, format="json")
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_cancel_queued_run(self):
        run_id = self._make_queued_run()
        resp = self.client.post(f"/api/projects/{self.pid}/audit-runs/{run_id}/cancel/")
        assert resp.status_code == 200, f"Cancel failed: {resp.status_code} {resp.content}"
        self.assertEqual(resp.json()["status"], "cancelled")

    def test_cancel_terminal_run_returns_409(self):
        run_id = self._make_queued_run("Terminal Run")
        # Cancel it first
        self.client.post(f"/api/projects/{self.pid}/audit-runs/{run_id}/cancel/")
        # Try again — should be 409
        resp = self.client.post(f"/api/projects/{self.pid}/audit-runs/{run_id}/cancel/")
        self.assertEqual(resp.status_code, 409)

    def test_cancel_unknown_run_returns_404(self):
        resp = self.client.post(f"/api/projects/{self.pid}/audit-runs/99999/cancel/")
        self.assertEqual(resp.status_code, 404)
