"""Tests for the durable SSE progress endpoint.

Verifies that the browser-facing event stream replays from Last-Event-ID and ends
on a terminal run event, using the real Postgres/SQLite AuditEvent rows (no live
Hatchet server required).
"""
from django.test import Client, TestCase

from audits.events import append_event


class AuditSSEStreamTest(TestCase):
    def setUp(self):
        from accounts.models import Project, ProjectMembership, User

        self.user = User.objects.create_user(username="sse-user", password="pw12345")
        self.project = Project.objects.create(name="P", slug="p")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client = Client()
        self.client.force_login(self.user)

    def _events_url(self, run_id):
        return f"/api/projects/{self.project.id}/audit-runs/{run_id}/events/"

    def _make_run(self):
        from audits.models import AuditRun
        from model_registry.models import ModelEndpoint
        from scenarios.models import (
            Scenario,
            ScenarioRevision,
            ScenarioSet,
            ScenarioSetVersion,
        )

        # Build minimal immutable inputs directly; SSE only reads events + status.
        target = ModelEndpoint.objects.create(
            project=self.project, display_name="t", provider="openai", base_url="http://x", model_id="m"
        )
        # A run needs a scenario_set_version FK; create the minimal chain.
        scenario = Scenario.objects.create(project=self.project, key="k", title="T")
        revision = ScenarioRevision.objects.create(
            scenario=scenario, revision=1, description="d", content_hash="h"
        )
        sset = ScenarioSet.objects.create(project=self.project, name="S")
        version = ScenarioSetVersion.objects.create(
            scenario_set=sset, version=1, content_hash="vh", scenario_count=1
        )
        from scenarios.models import ScenarioSetVersionItem

        ScenarioSetVersionItem.objects.create(version=version, scenario=scenario, revision=revision, position=1)

        run = AuditRun.objects.create(
            project=self.project,
            name="run",
            status=AuditRun.Status.QUEUED,
            scenario_set_version=version,
            target_endpoint=target,
            auditor_endpoint=target,
            judge_endpoint=target,
            target_config_snapshot={},
            auditor_config_snapshot={},
            judge_config_snapshot={},
            generation_parameters_snapshot={},
            simpleaudit_version="0.1.0",
            git_commit="abc123",
            total_scenarios=1,
            created_by=self.user,
        )
        return run

    def test_stream_replays_from_last_event_id_and_ends_on_terminal(self):
        run = self._make_run()
        e1 = append_event(run.id, "a", "scenario_attempted", {"attempt": 1})
        e2 = append_event(run.id, "a", "scenario_completed", {"attempt": 1})
        e3 = append_event(run.id, "_run", "run_completed", {"scenarios": 1})

        # Reconnect with Last-Event-ID == e1 -> should receive e2 and e3, then end.
        response = self.client.get(self._events_url(run.id), HTTP_LAST_EVENT_ID=str(e1))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")

        body = b"".join(response.streaming_content).decode()
        self.assertIn(f"id: {e2}", body)
        self.assertIn(f"id: {e3}", body)
        self.assertNotIn(f"id: {e1}", body)
        self.assertIn("event: run_completed", body)

    def test_stream_requires_project_access(self):
        run = self._make_run()
        from accounts.models import User

        outsider = User.objects.create_user(username="outsider", password="pw12345")
        client2 = Client()
        client2.force_login(outsider)
        response = client2.get(self._events_url(run.id))
        self.assertEqual(response.status_code, 403)
