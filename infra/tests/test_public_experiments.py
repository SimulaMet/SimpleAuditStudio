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

    # ── SEO metadata on public pages ─────────────────────────────────────────

    def test_anon_public_experiment_detail_has_seo_metadata(self):
        resp = self.anon.get(f"/experiments/{self.public_exp.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # Indexable by search engines.
        self.assertIn('<meta name="robots" content="index,follow">', html)
        # Share card: title, description, and an absolute og:image with
        # declared dimensions (so crawlers precache the image synchronously).
        self.assertIn(
            '<meta property="og:title" content="Public Exp — SimpleAudit Studio">', html
        )
        self.assertIn('<meta property="og:description" content="', html)
        self.assertIn(
            '<meta property="og:image" content="http://localhost/static/og-image.png">', html
        )
        self.assertIn('<meta property="og:image:width" content="1200">', html)
        self.assertIn('<meta property="og:image:height" content="630">', html)
        # Canonical points at the experiment URL itself.
        self.assertIn(
            f'<link rel="canonical" href="http://localhost/experiments/{self.public_exp.id}/">', html
        )

    def test_anon_public_gallery_has_seo_metadata(self):
        resp = self.anon.get("/public-experiments/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('<meta name="robots" content="index,follow">', html)
        self.assertIn(
            '<meta property="og:image" content="http://localhost/static/og-image.png">', html
        )
        self.assertIn('<meta property="og:image:width" content="1200">', html)
        self.assertIn('<meta property="og:image:height" content="630">', html)
        self.assertIn(
            '<link rel="canonical" href="http://localhost/public-experiments/">', html
        )

    def test_signed_in_experiment_detail_stays_noindex(self):
        # Signed-in users see their workspace-scoped page, which must not be
        # indexed (only anonymous-public pages carry indexable metadata).
        resp = self.admin_client.get(f"/experiments/{self.public_exp.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<meta name="robots" content="noindex,nofollow">', resp.content.decode())

    def test_sitemap_lists_public_experiment_pages(self):
        resp = self.anon.get("/sitemap.xml")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(f"<loc>http://localhost/experiments/{self.public_exp.id}/</loc>", body)
        # Private experiments are never listed.
        self.assertNotIn(f"/experiments/{self.private_exp.id}/", body)
        self.assertIn("<loc>http://localhost/public-experiments/</loc>", body)

    def test_login_page_has_seo_metadata(self):
        # Unauthenticated visitors (and bots) land on /login/ when following a
        # shared link, so it needs share-card metadata too. The canonical points
        # at the public gallery — the indexable equivalent of this page.
        resp = self.anon.get("/login/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('<meta name="robots" content="index,follow">', html)
        self.assertIn(
            '<meta property="og:image" content="http://localhost/static/og-image.png">', html
        )
        self.assertIn('<meta property="og:image:width" content="1200">', html)
        self.assertIn('<meta property="og:image:height" content="630">', html)
        self.assertIn(
            '<link rel="canonical" href="http://localhost/public-experiments/">', html
        )

    def test_robots_allows_social_crawlers_to_fetch_share_image(self):
        resp = self.anon.get("/robots.txt")
        self.assertEqual(resp.status_code, 200)
        lines = [ln.strip() for ln in resp.content.decode().splitlines()]
        allow_og = "Allow: /static/og-image.png"
        disallow_static = "Disallow: /static/"
        # The more specific Allow must come before the blanket Disallow so the
        # longest-match precedence resolves to "allowed".
        self.assertIn(allow_og, lines)
        self.assertIn(disallow_static, lines)
        self.assertLess(lines.index(allow_og), lines.index(disallow_static))

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
