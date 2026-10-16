"""Tests for ``GET /runs/<id>/export/?format=simpleaudit``.

The export reassembles a finished run from its stored ``ScenarioResult`` rows
into the engine's own ``AuditResults`` JSON, so the tests round-trip it through
the engine: results stored from ``AuditResult.to_dict()`` must come back out as
the document ``AuditResults.to_dict()`` builds, and load with ``AuditResults.load``.
"""
import json
import tempfile
from pathlib import Path

from django.test import Client, TestCase
from django.utils import timezone
from simpleaudit.results import AuditResult, AuditResults

from audits.events import ScenarioResult
from audits.models import AuditRun
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    ScenarioFactory,
    ScenarioSetVersionFactory,
    ScenarioSetVersionItemFactory,
    UserFactory,
)


def _engine_result(name: str, severity: str) -> AuditResult:
    return AuditResult(
        scenario_name=name,
        scenario_description=f"{name} description",
        conversation=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
        severity=severity,
        issues_found=[] if severity == "pass" else [f"{name} issue"],
        positive_behaviors=["polite"],
        summary=f"{name} summary",
        recommendations=[] if severity == "pass" else [f"{name} fix"],
        judgment={"severity": severity},
        target_input_tokens=11,
        target_output_tokens=7,
    )


class RunExportSimpleAuditTest(TestCase):
    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=pw)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()

        version = ScenarioSetVersionFactory()
        self.run = AuditRunFactory(
            project=self.project,
            scenario_set_version=version,
            status=AuditRun.Status.COMPLETED,
            finished_at=timezone.now(),
        )
        self.engine_results = [_engine_result("Alpha", "pass"), _engine_result("Beta", "high")]
        for result in self.engine_results:
            item = ScenarioSetVersionItemFactory(version=version, scenario=ScenarioFactory(title=result.scenario_name))
            payload = result.to_dict()
            # Studio adds bookkeeping keys that are not part of the engine's AuditResult.
            payload["_language"] = "en"
            payload["trace_ids"] = ["abc"]
            ScenarioResult.objects.create(
                run_id=self.run.id, version_item_id=str(item.pk), status="completed", result=payload
            )

    def _export(self, run=None):
        return self.client.get(f"/runs/{(run or self.run).id}/export/?format=simpleaudit")

    def test_round_trip_matches_engine_document(self):
        resp = self._export()

        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"audit_{self.run.id}_simpleaudit.json", resp["Content-Disposition"])
        document = json.loads(resp.content)
        expected = AuditResults(self.engine_results).to_dict()
        for key in ("summary", "issues", "recommendations", "results"):
            self.assertEqual(document[key], expected[key], key)
        self.assertEqual(document["summary"]["total_scenarios"], 2)

    def test_export_loads_with_the_engine(self):
        document = json.loads(self._export().content)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = AuditResults.load(str(path))

        self.assertEqual([r.to_dict() for r in loaded.results], [r.to_dict() for r in self.engine_results])

    def test_run_provenance_is_included(self):
        document = json.loads(self._export().content)

        self.assertEqual(document["run"]["id"], self.run.id)
        self.assertEqual(document["run"]["simpleaudit_version"], self.run.simpleaudit_version)
        self.assertEqual(document["run"]["git_commit"], self.run.git_commit)
        self.assertEqual(document["timestamp"], self.run.finished_at.isoformat())

    def test_failed_scenario_is_exported_as_error(self):
        item = ScenarioSetVersionItemFactory(version=self.run.scenario_set_version, scenario=ScenarioFactory(title="Gamma"))
        ScenarioResult.objects.create(
            run_id=self.run.id, version_item_id=str(item.pk), status="failed", result={"error": "target timed out"}
        )

        document = json.loads(self._export().content)

        gamma = document["results"][-1]
        self.assertEqual(gamma["scenario_name"], "Gamma")
        self.assertEqual(gamma["severity"], "ERROR")
        self.assertEqual(gamma["summary"], "target timed out")

    def test_unfinished_run_is_a_conflict(self):
        run = AuditRunFactory(project=self.project, status=AuditRun.Status.AUDITING)

        self.assertEqual(self._export(run).status_code, 409)

    def test_run_with_repetitions_is_not_supported(self):
        run = AuditRunFactory(
            project=self.project, status=AuditRun.Status.COMPLETED, generation_parameters_snapshot={"n_repetitions": 3}
        )

        self.assertEqual(self._export(run).status_code, 422)

    def test_other_projects_run_is_not_found(self):
        other = AuditRunFactory(project=ProjectFactory(), status=AuditRun.Status.COMPLETED)

        self.assertEqual(self._export(other).status_code, 404)

    def test_login_is_required(self):
        resp = Client(SERVER_NAME="localhost").get(f"/runs/{self.run.id}/export/?format=simpleaudit")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_existing_json_export_is_unchanged(self):
        resp = self.client.get(f"/runs/{self.run.id}/export/?format=json")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["scenario_name"] for row in json.loads(resp.content)], ["Alpha", "Beta"])
