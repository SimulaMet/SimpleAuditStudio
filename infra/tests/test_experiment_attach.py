"""Experiments list delete, and New Experiment launching into an existing experiment.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_experiment_attach
"""
from django.test import Client

from audits.models import AuditRun, Experiment
from infra.tests.factories import MembershipFactory, UserFactory
from infra.tests.test_experiments import _ExperimentBase


class ExperimentListDeleteTests(_ExperimentBase):
    def test_list_shows_delete_and_deletes_keeping_runs(self):
        self.client.post("/experiments/new/", self._design(target_model=[self.t1.id], max_turns="3"))
        exp = Experiment.objects.get(project=self.project)
        page = self.client.get("/experiments/")
        self.assertContains(page, f"/experiments/{exp.id}/delete/")
        resp = self.client.post(f"/experiments/{exp.id}/delete/", {"next": "list"})
        self.assertRedirects(resp, "/experiments/", fetch_redirect_response=False)
        self.assertFalse(Experiment.objects.filter(pk=exp.pk).exists())
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 1)

    def test_viewer_sees_no_delete(self):
        exp = Experiment.objects.create(project=self.project, name="E", created_by=self.user)
        viewer = UserFactory()
        viewer.set_password("pw")
        viewer.save()
        MembershipFactory(user=viewer, project=self.project, role="viewer")
        c = Client()
        c.login(username=viewer.username, password="pw")
        page = c.get("/experiments/")
        self.assertNotContains(page, f"/experiments/{exp.id}/delete/")


class LaunchIntoExistingTests(_ExperimentBase):
    def setUp(self):
        super().setUp()
        self.client.post("/experiments/new/", self._design(target_model=[self.t1.id], max_turns="3", name="Base"))
        self.exp = Experiment.objects.get(project=self.project)

    def test_design_page_offers_existing_experiments_and_prefills_into(self):
        page = self.client.get(f"/experiments/new/?into={self.exp.id}")
        self.assertContains(page, 'name="into_experiment"')
        self.assertContains(page, f'value="{self.exp.id}" data-public="" selected')

    def test_detail_page_links_to_new_runs(self):
        page = self.client.get(f"/experiments/{self.exp.id}/")
        self.assertContains(page, f"/experiments/new/?into={self.exp.id}")

    def test_single_run_joins_existing(self):
        resp = self.client.post("/experiments/new/", self._design(
            target_model=[self.t2.id], max_turns="3", into_experiment=str(self.exp.id)))
        self.assertRedirects(resp, f"/experiments/{self.exp.id}/", fetch_redirect_response=False)
        self.assertEqual(Experiment.objects.filter(project=self.project).count(), 1)
        self.assertEqual(self.exp.runs.count(), 2)
        self.assertEqual(Experiment.objects.get(pk=self.exp.pk).name, "Base")

    def test_multi_run_review_and_launch_join_existing(self):
        review = self.client.post("/experiments/new/", self._design(into_experiment=str(self.exp.id)))
        self.assertEqual(review.context["into_experiment"], self.exp)
        self.assertContains(review, "Adding to experiment")
        resp = self.client.post("/experiments/new/", self._review_to_launch_payload(review))
        self.assertRedirects(resp, f"/experiments/{self.exp.id}/", fetch_redirect_response=False)
        self.assertEqual(Experiment.objects.filter(project=self.project).count(), 1)
        self.assertEqual(self.exp.runs.count(), 5)

    def test_public_experiment_needs_confirmation(self):
        self.exp.visibility = Experiment.Visibility.PUBLIC
        self.exp.save()
        resp = self.client.post("/experiments/new/", self._design(
            target_model=[self.t2.id], max_turns="3", into_experiment=str(self.exp.id)))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Confirm the change")
        self.assertEqual(self.exp.runs.count(), 1)
        self.client.post("/experiments/new/", self._design(
            target_model=[self.t2.id], max_turns="3", into_experiment=str(self.exp.id), into_confirm_public="1"))
        self.assertEqual(self.exp.runs.count(), 2)

    def test_other_workspace_experiment_rejected(self):
        from infra.tests.factories import ProjectFactory

        other = Experiment.objects.create(project=ProjectFactory(), name="Other")
        resp = self.client.post("/experiments/new/", self._design(
            target_model=[self.t2.id], max_turns="3", into_experiment=str(other.id)))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "no longer exists")
        self.assertEqual(other.runs.count(), 0)
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 1)
