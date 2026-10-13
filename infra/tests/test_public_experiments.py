"""Tests for public experiments (anonymous read access + admin-gated visibility).

An experiment's ``visibility`` controls who may view it and its runs/results:
- PRIVATE (default): only members of the owning workspace.
- PUBLIC: readable without login, across every project.

Only admins may change visibility. Anonymous users must never be able to see a
private experiment/run/result, even by guessing ids (they get 404, not 403, so
existence is not leaked).
"""
from django.test import Client, TestCase

from audits.models import AuditRun, Experiment
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    ScenarioResultFactory,
    UserFactory,
)


def _make_experiment(project, name="Exp", visibility=Experiment.Visibility.PRIVATE):
    return Experiment.objects.create(project=project, name=name, visibility=visibility)


class PublicExperimentsTest(TestCase):
    def setUp(self):
        # Admin user in project A (owns a public + a private experiment).
        self.admin = UserFactory()
        self.project_a = ProjectFactory(name="Alpha", slug="alpha")
        MembershipFactory(user=self.admin, project=self.project_a, role="admin")

        # A non-admin member (auditor) of project A — can view but not toggle.
        self.auditor = UserFactory()
        MembershipFactory(user=self.auditor, project=self.project_a, role="auditor")

        # A member of an unrelated project B.
        self.other = UserFactory()
        self.project_b = ProjectFactory(name="Beta", slug="beta")
        MembershipFactory(user=self.other, project=self.project_b, role="admin")

        self.public_exp = _make_experiment(self.project_a, "Public Exp", Experiment.Visibility.PUBLIC)
        self.private_exp = _make_experiment(self.project_a, "Private Exp", Experiment.Visibility.PRIVATE)
        # A run attached to each, plus one standalone run (no experiment).
        self.public_run = AuditRunFactory(
            project=self.project_a, status=AuditRun.Status.COMPLETED, experiment=self.public_exp
        )
        self.private_run = AuditRunFactory(
            project=self.project_a, status=AuditRun.Status.COMPLETED, experiment=self.private_exp
        )
        self.standalone_run = AuditRunFactory(
            project=self.project_a, status=AuditRun.Status.COMPLETED, experiment=None
        )
        self.result = ScenarioResultFactory(run_id=self.public_run.id)

        self.anon = Client(SERVER_NAME="localhost")
        self.admin_client = Client(SERVER_NAME="localhost")
        self.admin_client.force_login(self.admin)
        self.admin_client.session["active_project_id"] = self.project_a.pk
        self.admin_client.session.save()
        self.auditor_client = Client(SERVER_NAME="localhost")
        self.auditor_client.force_login(self.auditor)
        self.auditor_client.session["active_project_id"] = self.project_a.pk
        self.auditor_client.session.save()
        self.other_client = Client(SERVER_NAME="localhost")
        self.other_client.force_login(self.other)
        self.other_client.session["active_project_id"] = self.project_b.pk
        self.other_client.session.save()

    # ── Anonymous read access ────────────────────────────────────────────────

    def test_anon_public_list_shows_only_public(self):
        resp = self.anon.get("/public-experiments/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Public Exp", resp.content)
        self.assertNotIn(b"Private Exp", resp.content)

    def test_anon_can_view_public_experiment_detail(self):
        resp = self.anon.get(f"/experiments/{self.public_exp.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_anon_cannot_view_private_experiment_detail(self):
        resp = self.anon.get(f"/experiments/{self.private_exp.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_anon_can_view_public_run_detail(self):
        resp = self.anon.get(f"/runs/{self.public_run.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_anon_cannot_view_private_run_detail(self):
        resp = self.anon.get(f"/runs/{self.private_run.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_anon_cannot_view_standalone_run_detail(self):
        # A run with no parent experiment is never publicly readable.
        resp = self.anon.get(f"/runs/{self.standalone_run.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_anon_can_view_public_result_detail(self):
        resp = self.anon.get(f"/runs/{self.public_run.id}/results/{self.result.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_anon_cannot_view_result_of_private_run(self):
        priv_result = ScenarioResultFactory(run_id=self.private_run.id)
        resp = self.anon.get(f"/runs/{self.private_run.id}/results/{priv_result.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_anon_experiments_page_redirects_to_public_gallery(self):
        # Anonymous visitors hitting /experiments/ are routed to the dedicated
        # public gallery (proper no-login layout), which lists only public ones.
        resp = self.anon.get("/experiments/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/public-experiments/", resp.headers["Location"])
        gallery = self.anon.get("/public-experiments/")
        self.assertEqual(gallery.status_code, 200)
        self.assertIn(b"Public Exp", gallery.content)
        self.assertNotIn(b"Private Exp", gallery.content)

    # ── Authenticated behavior unchanged (no cross-project leak) ─────────────

    def test_member_sees_own_project_experiments(self):
        resp = self.admin_client.get("/experiments/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Public Exp", resp.content)
        self.assertIn(b"Private Exp", resp.content)

    def test_other_project_member_cannot_see_private_cross_project(self):
        # Project B's admin must not see project A's private experiment by id.
        resp = self.other_client.get(f"/experiments/{self.private_exp.id}/")
        self.assertEqual(resp.status_code, 404)
        resp = self.other_client.get(f"/experiments/{self.public_exp.id}/")
        self.assertEqual(resp.status_code, 404)

    # ── Visibility toggling (admins only) ────────────────────────────────────

    def test_admin_can_make_experiment_public(self):
        resp = self.admin_client.post(
            f"/experiments/{self.private_exp.id}/set-visibility/", {"visibility": "public"}
        )
        self.assertEqual(resp.status_code, 302)
        self.private_exp.refresh_from_db()
        self.assertEqual(self.private_exp.visibility, Experiment.Visibility.PUBLIC)
        # Now anonymous can see it.
        self.assertEqual(self.anon.get(f"/experiments/{self.private_exp.id}/").status_code, 200)

    def test_admin_can_make_experiment_private(self):
        resp = self.admin_client.post(
            f"/experiments/{self.public_exp.id}/set-visibility/", {"visibility": "private"}
        )
        self.assertEqual(resp.status_code, 302)
        self.public_exp.refresh_from_db()
        self.assertEqual(self.public_exp.visibility, Experiment.Visibility.PRIVATE)
        self.assertEqual(self.anon.get(f"/experiments/{self.public_exp.id}/").status_code, 404)

    def test_non_admin_cannot_toggle_visibility(self):
        resp = self.auditor_client.post(
            f"/experiments/{self.private_exp.id}/set-visibility/", {"visibility": "public"}
        )
        self.assertEqual(resp.status_code, 403)
        self.private_exp.refresh_from_db()
        self.assertEqual(self.private_exp.visibility, Experiment.Visibility.PRIVATE)

    def test_other_project_admin_cannot_toggle_foreign_experiment(self):
        resp = self.other_client.post(
            f"/experiments/{self.private_exp.id}/set-visibility/", {"visibility": "public"}
        )
        # Scoped to the active workspace -> not found (404), never mutated.
        self.assertEqual(resp.status_code, 404)
        self.private_exp.refresh_from_db()
        self.assertEqual(self.private_exp.visibility, Experiment.Visibility.PRIVATE)

    def test_anonymous_cannot_toggle_visibility(self):
        resp = self.anon.post(f"/experiments/{self.private_exp.id}/set-visibility/", {"visibility": "public"})
        # LoginRequiredMixin redirects anonymous users to the login page.
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.headers["Location"])
        self.private_exp.refresh_from_db()
        self.assertEqual(self.private_exp.visibility, Experiment.Visibility.PRIVATE)

    def test_invalid_visibility_value_rejected(self):
        resp = self.admin_client.post(
            f"/experiments/{self.private_exp.id}/set-visibility/", {"visibility": "bogus"}
        )
        self.assertEqual(resp.status_code, 302)
        self.private_exp.refresh_from_db()
        self.assertEqual(self.private_exp.visibility, Experiment.Visibility.PRIVATE)
