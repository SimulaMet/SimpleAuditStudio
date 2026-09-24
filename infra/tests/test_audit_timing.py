"""Tests for audit run timing display (started/finished/duration).

The audit detail page must show a duration whenever both started_at and
finished_at are present, and must not show a bogus duration when either is
missing (e.g. a run that was cancelled before any scenario executed).
"""
from django.test import Client, TestCase
from django.utils import timezone

from audits.models import AuditRun
from infra.tests.factories import (
    UserFactory, ProjectFactory, MembershipFactory, AuditRunFactory,
)


class AuditTimingDisplayTest(TestCase):
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

    def test_duration_shown_when_started_and_finished(self):
        run = AuditRunFactory(project=self.project, status=AuditRun.Status.COMPLETED)
        started = timezone.now() - timezone.timedelta(minutes=12)
        run.started_at = started
        run.finished_at = started + timezone.timedelta(minutes=12)
        run.save(update_fields=["started_at", "finished_at"])

        resp = self.client.get(f"/audits/{run.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Duration", html)
        self.assertIn("12 min", html)

    def test_no_duration_when_started_missing(self):
        run = AuditRunFactory(project=self.project, status=AuditRun.Status.COMPLETED)
        run.started_at = None
        run.finished_at = timezone.now()
        run.save(update_fields=["started_at", "finished_at"])

        resp = self.client.get(f"/audits/{run.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn("Duration", html)

    def test_no_duration_when_finished_missing(self):
        run = AuditRunFactory(project=self.project, status=AuditRun.Status.QUEUED)
        run.started_at = timezone.now()
        run.finished_at = None
        run.save(update_fields=["started_at", "finished_at"])

        resp = self.client.get(f"/audits/{run.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn("Duration", html)
