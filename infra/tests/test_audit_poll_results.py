"""Tests for the one-shot JSON event-poll endpoint and the per-scenario results view.

The SPA cannot use ``EventSource`` under token auth, so it polls
``/events/poll/?after_id=N`` expecting a plain JSON array of durable events. These
tests pin that contract (cursor semantics + terminal kinds) and verify the results
view joins each stored result with its frozen scenario identity.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from audits.events import append_event, upsert_scenario_result


class _Base(TestCase):
    def setUp(self):
        from accounts.models import Project, ProjectMembership, User

        self.user = User.objects.create_user(username="poll-user", password="pw12345")
        self.project = Project.objects.create(name="P", slug="p")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _make_run(self):
        from audits.models import AuditRun
        from model_registry.models import ModelEndpoint
        from scenarios.models import (
            Scenario,
            ScenarioRevision,
            ScenarioSet,
            ScenarioSetVersion,
            ScenarioSetVersionItem,
        )

        target = ModelEndpoint.objects.create(
            project=self.project, display_name="t", provider="openai", base_url="http://x", model_id="m"
        )
        scenario = Scenario.objects.create(project=self.project, key="k", title="T")
        revision = ScenarioRevision.objects.create(scenario=scenario, revision=1, description="d", content_hash="h")
        sset = ScenarioSet.objects.create(project=self.project, name="S")
        version = ScenarioSetVersion.objects.create(scenario_set=sset, version=1, content_hash="vh", scenario_count=1)
        item = ScenarioSetVersionItem.objects.create(version=version, scenario=scenario, revision=revision, position=1)

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
        return run, item


class PollEventsTest(_Base):
    def _url(self, run_id):
        return f"/api/projects/{self.project.id}/audit-runs/{run_id}/events/poll/"

    def test_poll_returns_only_new_events_after_cursor(self):
        run, _ = self._make_run()
        e1 = append_event(run.id, "a", "scenario_attempted", {"attempt": 1})
        e2 = append_event(run.id, "a", "scenario_completed", {"attempt": 1})

        # after_id=0 -> both events, oldest first.
        resp = self.client.get(self._url(run.id), {"after_id": "0"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual([e["id"] for e in data], [e1, e2])
        self.assertEqual(data[0]["kind"], "scenario_attempted")
        self.assertIn("payload", data[0])

        # after_id=e1 -> only e2.
        resp = self.client.get(self._url(run.id), {"after_id": str(e1)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([e["id"] for e in resp.json()], [e2])

    def test_poll_requires_project_access(self):
        run, _ = self._make_run()
        from accounts.models import User

        outsider = User.objects.create_user(username="outsider2", password="pw12345")
        client2 = APIClient()
        client2.force_authenticate(user=outsider)
        resp = client2.get(self._url(run.id))
        self.assertEqual(resp.status_code, 403)

    def test_poll_unknown_run_404(self):
        resp = self.client.get(self._url(99999))
        self.assertEqual(resp.status_code, 404)


class ResultsViewTest(_Base):
    def _url(self, run_id):
        return f"/api/projects/{self.project.id}/audit-runs/{run_id}/results/"

    def test_results_join_with_frozen_scenario_identity(self):
        run, item = self._make_run()
        upsert_scenario_result(
            run_id=run.id,
            version_item_id=str(item.id),
            status="completed",
            attempts=1,
            result={"severity": "low", "summary": "ok"},
        )
        resp = self.client.get(self._url(run.id))
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["result"]["severity"], "low")
        # Frozen identity from the pinned version item.
        self.assertEqual(row["scenario_key"], "k")
        self.assertEqual(row["scenario_title"], "T")
        self.assertEqual(row["position"], 1)

    def test_results_empty_when_nothing_executed(self):
        run, _ = self._make_run()
        resp = self.client.get(self._url(run.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_results_unknown_run_404(self):
        resp = self.client.get(self._url(99999))
        self.assertEqual(resp.status_code, 404)
