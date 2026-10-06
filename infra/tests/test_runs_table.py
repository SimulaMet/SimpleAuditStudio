"""Tests for the interactive dashboard grid endpoints.

The dashboard page renders an empty grid; rows come from /runs/data/ (paged,
sorted, filtered server-side), bulk actions go to /runs/bulk/, the column
layout is saved through /me/preferences/, and /runs/export.csv follows the
same filters as the grid.
"""
import json

from django.test import Client, TestCase

from audits.models import AuditRun
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    UserFactory,
)


class GridTestBase(TestCase):
    role = "admin"

    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role=self.role)
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def data(self, query=""):
        resp = self.client.get("/runs/data/" + query)
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def ids(self, query=""):
        return [row["id"] for row in self.data(query)["data"]]

    def post_json(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")


class DashboardPageTests(GridTestBase):
    def test_page_renders_grid_and_filters(self):
        run = AuditRunFactory(project=self.project)
        html = self.client.get("/").content.decode()
        self.assertIn('id="runs-grid"', html)
        self.assertIn('data-multi="target"', html)
        self.assertIn(run.target_model.display_name, html)   # filter option
        self.assertIn('id="column-layout"', html)

    def test_stat_cards_add_up(self):
        for status in ("completed", "failed", "cancelled", "auditing"):
            AuditRunFactory(project=self.project, status=status)
        AuditRunFactory(project=self.project, archived=True)
        stats = self.client.get("/").context["stats"]
        self.assertEqual(stats["total"], stats["active"] + stats["completed"] + stats["failed"] + stats["cancelled"])
        self.assertEqual((stats["total"], stats["cancelled"], stats["archived"]), (4, 1, 1))

    def test_saved_layout_is_embedded_once_not_double_encoded(self):
        self.user.preferences = {"dashboard_columns": [{"field": "auditor", "visible": True}]}
        self.user.save()
        html = self.client.get("/").content.decode()
        self.assertIn('[{"field": "auditor", "visible": true}]</script>', html)


class RunsDataTests(GridTestBase):
    def test_rows_and_paging(self):
        runs = [AuditRunFactory(project=self.project) for _ in range(12)]
        page = self.data("?size=10&page=2")
        self.assertEqual(page["total"], 12)
        self.assertEqual(page["last_page"], 2)
        self.assertEqual(len(page["data"]), 2)
        row = self.data("?size=10")["data"][0]
        self.assertEqual(row["id"], runs[-1].id)   # newest first by default
        for key in ("name", "url", "status", "pass_rate", "target", "scenario_set", "reps", "created_at"):
            self.assertIn(key, row)
        for key in ("agentic_status", "agentic_pass_rate", "agentic_inconclusive_rate"):
            self.assertIn(key, row)

    def test_agentic_metrics_are_exposed(self):
        run = AuditRunFactory(
            project=self.project,
            summary_metrics={
                "agentic_pass_rate": 0.25,
                "inconclusive_rate": 0.5,
                "agentic_evaluation": {"total": 8, "passed": 2, "failed": 2, "inconclusive": 4},
            },
        )
        row = next(item for item in self.data()["data"] if item["id"] == run.id)
        self.assertEqual(row["agentic_status"], "INCONCLUSIVE")
        self.assertEqual(row["agentic_pass_rate"], 0.25)
        self.assertEqual(row["agentic_inconclusive_rate"], 0.5)

    def test_invalid_page_size_falls_back(self):
        AuditRunFactory(project=self.project)
        self.assertEqual(len(self.data("?size=999&page=abc")["data"]), 1)

    def test_other_workspace_runs_are_excluded(self):
        mine = AuditRunFactory(project=self.project)
        theirs = AuditRunFactory()
        ids = self.ids()
        self.assertIn(mine.id, ids)
        self.assertNotIn(theirs.id, ids)

    def test_status_filters(self):
        done = AuditRunFactory(project=self.project, status=AuditRun.Status.COMPLETED)
        failed = AuditRunFactory(project=self.project, status=AuditRun.Status.FAILED)
        running = AuditRunFactory(project=self.project, status=AuditRun.Status.AUDITING)
        archived = AuditRunFactory(project=self.project, archived=True)
        self.assertEqual(set(self.ids()), {done.id, failed.id, running.id})
        self.assertEqual(self.ids("?status=failed"), [failed.id])
        self.assertEqual(self.ids("?status=active"), [running.id])
        self.assertEqual(self.ids("?status=archived"), [archived.id])

    def test_search_by_name_and_id(self):
        a = AuditRunFactory(project=self.project, name="Llama sweep")
        b = AuditRunFactory(project=self.project, name="Other")
        self.assertEqual(self.ids("?q=llama"), [a.id])
        self.assertIn(b.id, self.ids(f"?q=%23{b.id}"))

    def test_multi_value_filters(self):
        a = AuditRunFactory(project=self.project)
        b = AuditRunFactory(project=self.project)
        c = AuditRunFactory(project=self.project)
        self.assertEqual(set(self.ids(f"?target={a.target_model_id}&target={b.target_model_id}")), {a.id, b.id})
        set_id = c.scenario_set_version.scenario_set_id
        self.assertEqual(self.ids(f"?set={set_id}"), [c.id])

    def test_sort_whitelist(self):
        AuditRunFactory(project=self.project, name="b")
        AuditRunFactory(project=self.project, name="a")
        names = [r["name"] for r in self.data("?sort=name&dir=asc")["data"]]
        self.assertEqual(names, ["a", "b"])
        # Unknown sort fields fall back to newest first instead of erroring.
        self.assertEqual(len(self.data("?sort=__class__")["data"]), 2)

    def test_repetitions_reported(self):
        AuditRunFactory(project=self.project, generation_parameters_snapshot={"n_repetitions": 3})
        self.assertEqual(self.data()["data"][0]["reps"], 3)


class RunsBulkTests(GridTestBase):
    def test_archive_and_unarchive(self):
        runs = [AuditRunFactory(project=self.project) for _ in range(2)]
        resp = self.post_json("/runs/bulk/", {"action": "archive", "ids": [r.id for r in runs]})
        self.assertEqual(resp.json()["changed"], 2)
        self.assertEqual(AuditRun.objects.filter(archived=True).count(), 2)
        self.post_json("/runs/bulk/", {"action": "unarchive", "ids": [runs[0].id]})
        self.assertEqual(AuditRun.objects.filter(archived=True).count(), 1)

    def test_cancel_only_touches_active_runs(self):
        running = AuditRunFactory(project=self.project, status=AuditRun.Status.AUDITING)
        done = AuditRunFactory(project=self.project, status=AuditRun.Status.COMPLETED)
        resp = self.post_json("/runs/bulk/", {"action": "cancel", "ids": [running.id, done.id]})
        self.assertEqual(resp.json()["changed"], 1)
        running.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(running.status, AuditRun.Status.CANCELLED)
        self.assertEqual(done.status, AuditRun.Status.COMPLETED)

    def test_other_workspace_runs_untouched(self):
        theirs = AuditRunFactory()
        resp = self.post_json("/runs/bulk/", {"action": "archive", "ids": [theirs.id]})
        self.assertEqual(resp.json()["changed"], 0)
        theirs.refresh_from_db()
        self.assertFalse(theirs.archived)

    def test_unknown_action_and_bad_ids(self):
        self.assertEqual(self.post_json("/runs/bulk/", {"action": "delete", "ids": []}).status_code, 400)
        self.assertEqual(self.post_json("/runs/bulk/", {"action": "archive", "ids": ["x"]}).status_code, 400)


class RunsBulkViewerTests(GridTestBase):
    role = "viewer"

    def test_viewer_cannot_bulk_change(self):
        run = AuditRunFactory(project=self.project)
        resp = self.post_json("/runs/bulk/", {"action": "archive", "ids": [run.id]})
        self.assertEqual(resp.status_code, 403)
        run.refresh_from_db()
        self.assertFalse(run.archived)

    def test_viewer_can_read_grid(self):
        run = AuditRunFactory(project=self.project)
        self.assertEqual(self.ids(), [run.id])


class PreferenceTests(GridTestBase):
    def test_save_and_clear_layout(self):
        layout = [{"field": "auditor", "visible": True, "width": 180}]
        self.assertEqual(self.post_json("/me/preferences/", {"key": "dashboard_columns", "value": layout}).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences["dashboard_columns"], layout)
        self.post_json("/me/preferences/", {"key": "dashboard_columns", "value": None})
        self.user.refresh_from_db()
        self.assertNotIn("dashboard_columns", self.user.preferences)

    def test_unknown_key_rejected(self):
        resp = self.post_json("/me/preferences/", {"key": "is_superuser", "value": True})
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences, {})

    def test_oversized_value_rejected(self):
        resp = self.post_json("/me/preferences/", {"key": "dashboard_columns", "value": ["x" * 30_000]})
        self.assertEqual(resp.status_code, 400)

    def test_requires_login(self):
        self.client.logout()
        resp = self.post_json("/me/preferences/", {"key": "dashboard_columns", "value": []})
        self.assertNotEqual(resp.status_code, 200)


class RunsExportTests(GridTestBase):
    def test_export_follows_filters(self):
        keep = AuditRunFactory(project=self.project, name="Keep me")
        AuditRunFactory(project=self.project, name="Drop me", status=AuditRun.Status.FAILED)
        resp = self.client.get("/runs/export.csv?status=completed")
        self.assertEqual(resp["Content-Type"], "text/csv; charset=utf-8")
        body = resp.content.decode()
        self.assertTrue(body.startswith("id,name,status"))
        self.assertIn(f"{keep.id},Keep me", body)
        self.assertNotIn("Drop me", body)
