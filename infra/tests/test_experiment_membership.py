"""Editable experiment membership (runs added/removed after launch).

- A run can belong to several experiments (ExperimentRun join rows).
- Factors are computed from the member runs, so they follow edits.
- Admins and auditors edit membership; a public experiment needs an admin
  plus an explicit confirmation, since adding a run publishes it.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase

from audits.experiments import (
    MAX_RUNS_PER_EXPERIMENT,
    MembershipError,
    add_runs,
    remove_runs,
)
from audits.models import AuditRun, Experiment, ExperimentRun, Monitor
from infra.exceptions import StableAPIError
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    UserFactory,
)


def _run(project, **params):
    return AuditRunFactory(project=project, generation_parameters_snapshot={"max_turns": 5, **params})


class MembershipServiceTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.admin = UserFactory()
        self.auditor = UserFactory()
        self.viewer = UserFactory()
        MembershipFactory(user=self.admin, project=self.project, role="admin")
        MembershipFactory(user=self.auditor, project=self.project, role="auditor")
        MembershipFactory(user=self.viewer, project=self.project, role="viewer")
        self.exp = Experiment.objects.create(project=self.project, name="E")
        self.r1 = _run(self.project)
        self.r1.experiments.add(self.exp)

    def test_auditor_adds_run_and_factors_follow(self):
        same = {k: getattr(self.r1, k) for k in
                ("scenario_set_version", "target_model", "auditor_model", "judge_model", "judge_version")}
        r2 = AuditRunFactory(project=self.project, generation_parameters_snapshot={"max_turns": 9}, **same)
        self.assertEqual(self.exp.factors, [])
        self.assertEqual(add_runs(experiment=self.exp, user=self.auditor, run_ids=[r2.id]), 1)
        self.assertEqual(self.exp.factors, ["max_turns"])
        membership = ExperimentRun.objects.get(experiment=self.exp, run=r2)
        self.assertEqual(membership.added_by, self.auditor)
        # Removing it again drops the factor; the run itself is kept.
        self.assertEqual(remove_runs(experiment=self.exp, user=self.auditor, run_ids=[r2.id]), 1)
        self.assertEqual(self.exp.factors, [])
        self.assertTrue(AuditRun.objects.filter(pk=r2.pk).exists())

    def test_run_can_join_several_experiments(self):
        other = Experiment.objects.create(project=self.project, name="Other")
        add_runs(experiment=other, user=self.admin, run_ids=[self.r1.id])
        self.assertEqual(set(self.r1.experiments.all()), {self.exp, other})

    def test_adding_existing_member_is_a_noop(self):
        self.assertEqual(add_runs(experiment=self.exp, user=self.admin, run_ids=[self.r1.id]), 0)
        self.assertEqual(self.exp.memberships.count(), 1)

    def test_viewer_cannot_edit(self):
        r2 = _run(self.project)
        with self.assertRaises(StableAPIError):
            add_runs(experiment=self.exp, user=self.viewer, run_ids=[r2.id])
        with self.assertRaises(StableAPIError):
            remove_runs(experiment=self.exp, user=self.viewer, run_ids=[self.r1.id])

    def test_runs_from_another_workspace_are_rejected(self):
        foreign = _run(ProjectFactory())
        with self.assertRaises(MembershipError):
            add_runs(experiment=self.exp, user=self.admin, run_ids=[foreign.id])
        self.assertFalse(self.exp.runs.filter(pk=foreign.pk).exists())

    def test_public_experiment_needs_admin_and_confirmation(self):
        self.exp.visibility = Experiment.Visibility.PUBLIC
        self.exp.save()
        r2 = _run(self.project)
        with self.assertRaises(StableAPIError):
            add_runs(experiment=self.exp, user=self.auditor, run_ids=[r2.id], confirm_public=True)
        with self.assertRaises(MembershipError):
            add_runs(experiment=self.exp, user=self.admin, run_ids=[r2.id])
        self.assertEqual(add_runs(experiment=self.exp, user=self.admin, run_ids=[r2.id], confirm_public=True), 1)
        with self.assertRaises(MembershipError):
            remove_runs(experiment=self.exp, user=self.admin, run_ids=[r2.id])
        self.assertEqual(remove_runs(experiment=self.exp, user=self.admin, run_ids=[r2.id], confirm_public=True), 1)

    def test_cap_is_enforced(self):
        runs = [_run(self.project) for _ in range(MAX_RUNS_PER_EXPERIMENT)]
        with self.assertRaises(MembershipError):
            add_runs(experiment=self.exp, user=self.admin, run_ids=[r.id for r in runs])

    def test_monitor_runs_cannot_be_removed_one_by_one(self):
        monitor = Monitor.objects.create(
            project=self.project, name="M", scenario_set=self.r1.scenario_set_version.scenario_set,
            target_model=self.r1.target_model, auditor_model=self.r1.auditor_model,
            judge_model=self.r1.judge_model, judge=self.r1.judge_version.judge, experiment=self.exp,
        )
        AuditRun.objects.filter(pk=self.r1.pk).update(monitor=monitor)
        with self.assertRaises(MembershipError):
            remove_runs(experiment=self.exp, user=self.admin, run_ids=[self.r1.id])


class MembershipViewTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.admin = UserFactory()
        self.auditor = UserFactory()
        MembershipFactory(user=self.admin, project=self.project, role="admin")
        MembershipFactory(user=self.auditor, project=self.project, role="auditor")
        self.exp = Experiment.objects.create(project=self.project, name="E")
        self.run = _run(self.project)
        self.client = Client()
        self.client.force_login(self.auditor)

    def test_add_and_remove_via_post(self):
        resp = self.client.post(f"/experiments/{self.exp.id}/add-runs/", {"run_ids": [self.run.id]})
        self.assertRedirects(resp, f"/experiments/{self.exp.id}/", fetch_redirect_response=False)
        self.assertTrue(self.exp.runs.filter(pk=self.run.pk).exists())
        page = self.client.get(f"/experiments/{self.exp.id}/").content.decode()
        self.assertIn("remove-runs-form", page)
        self.assertIn("data-run-picker", page)
        self.client.post(f"/experiments/{self.exp.id}/remove-runs/", {"run_ids": [self.run.id]})
        self.assertFalse(self.exp.runs.exists())

    def test_run_page_offers_add_and_lists_experiments(self):
        page = self.client.get(f"/runs/{self.run.id}/").content.decode()
        self.assertIn(f"/experiments/{self.exp.id}/add-runs/", page)
        self.exp.runs.add(self.run)
        other = Experiment.objects.create(project=self.project, name="Second")
        other.runs.add(self.run)
        page = self.client.get(f"/runs/{self.run.id}/").content.decode()
        self.assertIn(f"/experiments/{self.exp.id}/", page)
        self.assertIn(f"/experiments/{other.id}/", page)

    def test_auditor_gets_403_on_public_experiment(self):
        self.exp.visibility = Experiment.Visibility.PUBLIC
        self.exp.save()
        resp = self.client.post(
            f"/experiments/{self.exp.id}/add-runs/", {"run_ids": [self.run.id], "confirm_public": "1"}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.exp.runs.exists())

    def test_adding_to_public_experiment_publishes_the_run(self):
        self.exp.visibility = Experiment.Visibility.PUBLIC
        self.exp.save()
        anon = Client()
        self.assertEqual(anon.get(f"/runs/{self.run.id}/").status_code, 404)
        admin = Client()
        admin.force_login(self.admin)
        admin.post(f"/experiments/{self.exp.id}/add-runs/", {"run_ids": [self.run.id], "confirm_public": "1"})
        self.assertEqual(anon.get(f"/runs/{self.run.id}/").status_code, 200)

    def test_dashboard_experiment_filter_and_row(self):
        self.exp.runs.add(self.run)
        other = Experiment.objects.create(project=self.project, name="Second")
        other.runs.add(self.run)
        data = self.client.get("/runs/data/", {"experiment": [self.exp.id, other.id]}).json()
        self.assertEqual([row["id"] for row in data["data"]], [self.run.id])   # no duplicate rows
        self.assertEqual({e["id"] for e in data["data"][0]["experiments"]}, {self.exp.id, other.id})

    def test_open_redirect_is_ignored(self):
        resp = self.client.post(
            f"/experiments/{self.exp.id}/add-runs/", {"run_ids": [self.run.id], "next": "https://evil.example/"}
        )
        self.assertRedirects(resp, f"/experiments/{self.exp.id}/", fetch_redirect_response=False)


class MembershipMigrationTests(TransactionTestCase):
    """0010 copies each run's old experiment FK into a membership row."""

    before = [("audits", "0009_auditrun_system_prompt_version")]
    after = [("audits", "0010_experiment_run_membership")]

    def test_fk_is_copied_to_memberships(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        old = executor.loader.project_state(self.before).apps
        run = AuditRunFactory()
        OldExperiment = old.get_model("audits", "Experiment")
        exp = OldExperiment.objects.create(project_id=run.project_id, name="Old", factors=["target"])
        old.get_model("audits", "AuditRun").objects.filter(pk=run.pk).update(experiment_id=exp.pk)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.after)
        new = executor.loader.project_state(self.after).apps
        Membership = new.get_model("audits", "ExperimentRun")
        row = Membership.objects.get()
        self.assertEqual((row.experiment_id, row.run_id), (exp.pk, run.pk))
        self.assertEqual(row.added_at, run.created_at)

        # Leave the schema at the latest state for other tests.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())


class DeleteExperimentTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.admin = UserFactory()
        self.auditor = UserFactory()
        self.viewer = UserFactory()
        MembershipFactory(user=self.admin, project=self.project, role="admin")
        MembershipFactory(user=self.auditor, project=self.project, role="auditor")
        MembershipFactory(user=self.viewer, project=self.project, role="viewer")
        self.exp = Experiment.objects.create(project=self.project, name="E")
        self.run = _run(self.project)
        self.exp.runs.add(self.run)

    def _client(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_auditor_deletes_private_experiment_runs_kept(self):
        resp = self._client(self.auditor).post(f"/experiments/{self.exp.id}/delete/")
        self.assertRedirects(resp, "/experiments/", fetch_redirect_response=False)
        self.assertFalse(Experiment.objects.filter(pk=self.exp.pk).exists())
        self.assertTrue(AuditRun.objects.filter(pk=self.run.pk).exists())
        self.assertFalse(ExperimentRun.objects.exists())

    def test_viewer_cannot_delete(self):
        resp = self._client(self.viewer).post(f"/experiments/{self.exp.id}/delete/")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Experiment.objects.filter(pk=self.exp.pk).exists())

    def test_public_needs_admin_and_confirmation(self):
        self.exp.visibility = Experiment.Visibility.PUBLIC
        self.exp.save()
        resp = self._client(self.auditor).post(f"/experiments/{self.exp.id}/delete/", {"confirm_public": "1"})
        self.assertEqual(resp.status_code, 403)
        self._client(self.admin).post(f"/experiments/{self.exp.id}/delete/")
        self.assertTrue(Experiment.objects.filter(pk=self.exp.pk).exists())   # no confirmation
        self._client(self.admin).post(f"/experiments/{self.exp.id}/delete/", {"confirm_public": "1"})
        self.assertFalse(Experiment.objects.filter(pk=self.exp.pk).exists())
        self.assertEqual(Client().get(f"/runs/{self.run.id}/").status_code, 404)   # unpublished

    def test_monitor_survives_detached(self):
        monitor = Monitor.objects.create(
            project=self.project, name="M", scenario_set=self.run.scenario_set_version.scenario_set,
            target_model=self.run.target_model, auditor_model=self.run.auditor_model,
            judge_model=self.run.judge_model, judge=self.run.judge_version.judge, experiment=self.exp,
        )
        self._client(self.admin).post(f"/experiments/{self.exp.id}/delete/")
        monitor.refresh_from_db()
        self.assertIsNone(monitor.experiment_id)
