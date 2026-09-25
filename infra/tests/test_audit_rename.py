"""Tests for renaming audit runs from the detail page.

Renaming is a display-only change: it updates the run's name label but does
not affect the frozen reproducibility manifest, results, or any other field.
"""
from django.test import Client, TestCase

from audits.models import AuditRun
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    UserFactory,
)


class AuditRenameTest(TestCase):
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
        self.run = AuditRunFactory(project=self.project, status=AuditRun.Status.COMPLETED, name="Original Name")

    def test_detail_page_shows_run_name(self):
        resp = self.client.get(f"/audits/{self.run.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Original Name", html)
        self.assertIn(f"Audit #{self.run.id}", html)

    def test_rename_via_post(self):
        resp = self.client.post(f"/audits/{self.run.id}/rename/", {"name": "New Name"})
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.name, "New Name")

    def test_rename_preserves_other_fields(self):
        original_status = self.run.status
        original_set_version = self.run.scenario_set_version_id
        original_target = self.run.target_endpoint_id

        self.client.post(f"/audits/{self.run.id}/rename/", {"name": "Changed"})
        self.run.refresh_from_db()

        self.assertEqual(self.run.status, original_status)
        self.assertEqual(self.run.scenario_set_version_id, original_set_version)
        self.assertEqual(self.run.target_endpoint_id, original_target)

    def test_rename_rejects_empty_name(self):
        resp = self.client.post(f"/audits/{self.run.id}/rename/", {"name": ""})
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.name, "Original Name")

    def test_rename_rejects_whitespace_only_name(self):
        resp = self.client.post(f"/audits/{self.run.id}/rename/", {"name": "   "})
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.name, "Original Name")

    def test_rename_strips_whitespace(self):
        resp = self.client.post(f"/audits/{self.run.id}/rename/", {"name": "  Padded Name  "})
        self.assertEqual(resp.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.name, "Padded Name")

    def test_rename_requires_project_access(self):
        other = UserFactory()
        other.set_password("otherpass" + "123")
        other.save()
        other_client = Client(SERVER_NAME="localhost")
        other_client.login(username=other.username, password="otherpass" + "123")
        other_client.session["project_id"] = self.project.pk
        other_client.session.save()

        resp = other_client.post(f"/audits/{self.run.id}/rename/", {"name": "Hacked"})
        self.assertIn(resp.status_code, (302, 403, 404))
        self.run.refresh_from_db()
        self.assertEqual(self.run.name, "Original Name")

    def test_rename_form_present_on_detail_page(self):
        resp = self.client.get(f"/audits/{self.run.id}/")
        html = resp.content.decode()
        self.assertIn(f"/audits/{self.run.id}/rename/", html)
        self.assertIn('id="rename-form"', html)
