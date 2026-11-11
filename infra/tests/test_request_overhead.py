"""Per-request overhead: membership lookups and token-auth session writes."""
from django.contrib.sessions.models import Session
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.authtoken.models import Token

from accounts.models import ProjectMembership
from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory


class MembershipQueriesTests(TestCase):
    def test_page_reads_memberships_once(self):
        user = UserFactory()
        for _ in range(3):
            MembershipFactory(user=user, project=ProjectFactory(), role=ProjectMembership.Role.ADMIN)
        self.client.force_login(user)
        self.client.get("/scenarios/")  # first hit sets the active workspace
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/scenarios/")
        self.assertEqual(resp.status_code, 200)
        membership_reads = [q["sql"] for q in ctx.captured_queries
                            if 'FROM "core_project_membership"' in q["sql"]]
        self.assertLessEqual(len(membership_reads), 2, membership_reads)

    def test_viewer_still_cannot_write(self):
        user = UserFactory()
        project = ProjectFactory()
        MembershipFactory(user=user, project=project, role=ProjectMembership.Role.VIEWER)
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = project.pk
        session.save()
        resp = self.client.get("/scenarios/")
        self.assertFalse(resp.context["can_write"])


class TokenAuthSessionTests(TestCase):
    def test_token_requests_create_no_sessions(self):
        user = UserFactory()
        MembershipFactory(user=user)
        token = Token.objects.create(user=user)
        for _ in range(3):
            resp = self.client.get("/scenarios/", HTTP_AUTHORIZATION=f"Token {token.key}")
            self.assertEqual(resp.status_code, 200)
            self.client.cookies.clear()
        self.assertEqual(Session.objects.count(), 0)
