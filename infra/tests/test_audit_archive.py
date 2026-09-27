"""Tests for archiving audit runs from the dashboard.

Archived runs are hidden from the default dashboard views (All/Active/
Completed/Failed) and the queue, but remain visible under the Archived
filter and fully accessible via their detail page. Archiving is a soft
toggle: it never deletes data and never affects the frozen manifest.
"""
from django.test import Client, TestCase

from audits.models import AuditRun
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    UserFactory,
)


class AuditArchiveTest(TestCase):
    def setUp(self):
        pw = "testpass" + "123"
        self.user = UserFactory()
        self.user.set_password(pw)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="owner")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=pw)
        self.client.session["project_id"] = self.project.pk
        self.client.session.save()
        self.run = AuditRunFactory(project=self.project, status=AuditRun.Status.COMPLETED)

    def test_archived_run_hidden_from_default_dashboard(self):
        self.run.archived = True
        self.run.save(update_fields=["archived"])

        resp = self.client.get("/dashboard/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(f"/audits/{self.run.id}/", resp.content.decode())

    def test_archived_run_visible_under_archived_filter(self):
        self.run.archived = True
        self.run.save(update_fields=["archived"])

        resp = self.client.get("/dashboard/?status=archived")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"/audits/{self.run.id}/", resp.content.decode())

    def test_unarchived_run_visible_in_default_dashboard(self):
        resp = self.client.get("/dashboard/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"/audits/{self.run.id}/", resp.content.decode())

    def test_archive_toggle_via_post(self):
        resp = self.client.post(f"/audits/{self.run.id}/archive/")
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertTrue(self.run.archived)

        resp = self.client.post(f"/audits/{self.run.id}/archive/")
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertFalse(self.run.archived)

    def test_archive_requires_project_access(self):
        other = UserFactory()
        other.set_password("otherpass" + "123")
        other.save()
        other_client = Client(SERVER_NAME="localhost")
        other_client.login(username=other.username, password="otherpass" + "123")
        other_client.session["project_id"] = self.project.pk
        other_client.session.save()

        resp = other_client.post(f"/audits/{self.run.id}/archive/")
        self.assertIn(resp.status_code, (302, 403, 404))
        self.run.refresh_from_db()
        self.assertFalse(self.run.archived)

    def test_dashboard_rows_have_clone_and_archive_actions(self):
        resp = self.client.get("/dashboard/")
        html = resp.content.decode()
        self.assertIn(f"/audits/new/?clone_from={self.run.id}", html)
        self.assertIn(f"/audits/{self.run.id}/archive/", html)

    def test_archived_run_detail_still_accessible(self):
        self.run.archived = True
        self.run.save(update_fields=["archived"])

        resp = self.client.get(f"/audits/{self.run.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_progress_column_shows_repetition_count(self):
        """Runs with n_repetitions > 1 show (xN) in the Progress column."""
        self.run.generation_parameters_snapshot = {"n_repetitions": 3}
        self.run.total_scenarios = 5
        self.run.completed_scenarios = 2
        self.run.save()

        resp = self.client.get("/dashboard/")
        html = resp.content.decode()
        self.assertIn('2/5<span class="text-gray-500">(x3)</span>', html)

    def test_progress_column_hides_repetition_count_for_single_rep(self):
        """Runs without repetitions (or n_repetitions == 1) show no (xN)."""
        self.run.generation_parameters_snapshot = {"temperature": 0.7}
        self.run.total_scenarios = 5
        self.run.completed_scenarios = 2
        self.run.save()

        resp = self.client.get("/dashboard/")
        html = resp.content.decode()
        self.assertNotIn("(x", html)
