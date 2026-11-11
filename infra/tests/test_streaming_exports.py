"""Run exports stream, and match what the non-streaming code produced."""
import csv
import io
import json

from django.test import TestCase

from audits.models import AuditRun
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    RepeatedScenarioResultFactory,
    ScenarioResultFactory,
    UserFactory,
)


class RunExportStreamingTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        user = UserFactory()
        MembershipFactory(user=user, project=self.project)
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = self.project.pk
        session.save()
        self.run = AuditRunFactory(project=self.project)
        ScenarioResultFactory(run_id=self.run.id, version_item_id="1", result={"severity": "pass", "summary": "=fine"})
        RepeatedScenarioResultFactory(run_id=self.run.id, version_item_id="2")
        ScenarioResultFactory(run_id=self.run.id, version_item_id="3", status="failed", result={})

    def _get(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.streaming)
        return b"".join(resp.streaming_content).decode()

    def test_csv_reads_projection_and_matches_json(self):
        rows = list(csv.reader(io.StringIO(self._get(f"/runs/{self.run.id}/export/?format=csv"))))
        doc = json.loads(self._get(f"/runs/{self.run.id}/export/?format=json"))
        self.assertEqual(rows[0], ["id", "scenario_name", "severity", "summary"])
        self.assertEqual((len(rows) - 1, len(doc)), (3, 3))
        for line, item in zip(rows[1:], doc, strict=True):
            self.assertEqual(line[2], item["severity"])
            # CSV cells are formula-escaped; the JSON keeps the raw text.
            self.assertEqual(line[3].lstrip("'"), item["summary"])
        self.assertEqual(rows[1][3], "'=fine")
        self.assertEqual(doc[2]["severity"], "failed")

    def test_runs_csv_streams_in_batches(self):
        AuditRun.objects.filter(pk=self.run.pk).update(name="Batch run")
        body = self._get("/runs/export.csv")
        self.assertIn("Batch run", body)
