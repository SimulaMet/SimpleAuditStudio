"""Smoke test: hit every page type, assert zero 5xx errors.

This is the safety net that catches template crashes, missing context vars,
and view bugs before they reach a user's browser.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 .venv/bin/python manage.py test infra.tests.test_smoke_all_pages
"""
from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    RegisteredModelFactory,
    RepeatedScenarioResultFactory,
    ScenarioFactory,
    ScenarioResultFactory,
    ScenarioRevisionFactory,
    ScenarioSetFactory,
    ScenarioSetVersionFactory,
    ScenarioSetVersionItemFactory,
    UserFactory,
)


class AllPagesSmokeTest(TestCase):
    """Visit every major page and assert no server errors."""

    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("testpass123")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="owner")

        # Scenario set with 2 versions (needed for diff view)
        sset = ScenarioSetFactory(project=self.project)
        scenario = ScenarioFactory(project=self.project)
        rev = ScenarioRevisionFactory(scenario=scenario)
        v1 = ScenarioSetVersionFactory(scenario_set=sset, version=1)
        v2 = ScenarioSetVersionFactory(scenario_set=sset, version=2)
        ScenarioSetVersionItemFactory(version=v1, scenario=scenario, revision=rev, position=1)
        item_v2 = ScenarioSetVersionItemFactory(version=v2, scenario=scenario, revision=rev, position=1)
        self.scenario_set = sset

        # Single-rep run
        model = RegisteredModelFactory(project=self.project)
        self.run = AuditRunFactory(
            project=self.project,
            scenario_set_version=v2,
            target_model=model,
            auditor_model=model,
            judge_model=model,
        )
        self.result = ScenarioResultFactory(run_id=self.run.pk, version_item_id=str(item_v2.pk))

        # Repeated run (n_repetitions=3)
        self.run_repeated = AuditRunFactory(
            project=self.project,
            scenario_set_version=v2,
            target_model=model,
            auditor_model=model,
            judge_model=model,
        )
        self.result_repeated = RepeatedScenarioResultFactory(run_id=self.run_repeated.pk, version_item_id=str(item_v2.pk))

        # Authenticated client
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password="testpass123")
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()

    def _ok(self, url, label):
        resp = self.client.get(url)
        self.assertLess(
            resp.status_code, 500,
            f"Server error ({resp.status_code}) on {label}: {url}\n{resp.content[:500]}",
        )

    def test_main_pages(self):
        self._ok("/scenarios/", "Scenario Library")
        self._ok("/models/", "Models")
        self._ok("/workspaces/", "Workspaces")
        self._ok("/profile/", "Profile")

    def test_admin_settings_requires_superuser(self):
        # Non-superuser gets 403, not a server error.
        resp = self.client.get("/admin-settings/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_settings_pages(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        creds = {"username": self.user.username, "password": "testpass123"}
        self.client.login(**creds)
        self._ok("/admin-settings/", "Admin Overview")
        self._ok("/admin-settings/?tab=workspaces", "Admin Workspaces")
        self._ok("/admin-settings/?tab=users", "Admin Users")
        self._ok(f"/admin-settings/?tab=workspaces&manage={self.project.id}", "Admin Members panel")

    def test_audit_detail(self):
        self._ok(f"/audits/{self.run.id}/", "Audit detail (single)")
        self._ok(f"/audits/{self.run_repeated.id}/", "Audit detail (repeated)")

    def test_audit_detail_live_state_updates_from_sse(self):
        """The Status card's State row must be live-updated from SSE events.

        Regression: the State row was rendered once server-side and never
        touched by the SSE handlers, so it stayed "Queued" until a manual
        page refresh even though the run had progressed to completed.
        """
        from audits.models import AuditRun

        active = AuditRunFactory(
            project=self.project,
            scenario_set_version=self.run.scenario_set_version,
            target_model=self.run.target_model,
            auditor_model=self.run.auditor_model,
            judge_model=self.run.judge_model,
            status=AuditRun.Status.QUEUED,
            total_scenarios=1,
            completed_scenarios=0,
        )
        resp = self.client.get(f"/audits/{active.id}/")
        html = resp.content.decode()
        # Live state element exists for non-terminal runs...
        self.assertIn('id="run-state"', html)
        # ...and is seeded with the current (queued) status.
        self.assertIn("Queued", html.split('id="run-state"', 1)[1][:200])
        # The SSE script updates it on terminal events.
        self.assertIn("updateRunState", html)

    def test_audit_exports(self):
        self._ok(f"/audits/{self.run.id}/export/?format=json", "Export JSON")
        self._ok(f"/audits/{self.run.id}/export/?format=csv", "Export CSV")
        self._ok(f"/audits/{self.run_repeated.id}/export/?format=json", "Export JSON (repeated)")

    def test_scenario_result_detail(self):
        self._ok(f"/audits/{self.run.id}/results/{self.result.id}/", "Result detail (single)")
        self._ok(f"/audits/{self.run_repeated.id}/results/{self.result_repeated.id}/", "Result detail (repeated)")

    def test_compare(self):
        self._ok(f"/compare/?a={self.run.id}&b={self.run_repeated.id}", "Compare")

    def test_scenario_diff(self):
        self._ok(f"/scenarios/diff/{self.scenario_set.id}/", "Scenario diff")

    def test_new_audit_form(self):
        self._ok("/new-audit/", "New Audit form")
