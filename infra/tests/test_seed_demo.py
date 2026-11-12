"""Demo seed: recorded runs arrive grouped into experiments.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_seed_demo
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from audits.models import AuditRun, Experiment
from infra.seed import seed_default_model_connections, seed_workspace
from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory


class DemoSeedExperimentsTests(TestCase):
    def setUp(self):
        self.user = UserFactory(is_superuser=True)
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        seed_workspace(self.project, self.user)
        seed_default_model_connections(self.project, self.user)

    def _seed(self, *args):
        call_command("seed_demo_audits", *args, project=self.project.id, stdout=StringIO())

    def _experiments(self):
        return {e.name: (e.factors, sorted(r.name for r in e.runs.all()))
                for e in Experiment.objects.filter(project=self.project)}

    def test_runs_are_grouped_with_the_factors_that_differ(self):
        self._seed()
        self.assertEqual(self._experiments(), {
            "Safety: does a hotter target change the verdicts?":
                (["params"], ["Demo: safety baseline", "Demo: safety elevated-temp"]),
            "Health vs RAG scenarios": (["scenario_set"], ["Demo: health baseline", "Demo: rag baseline"]),
            "Safety: graded by a decision model (Jev)":
                ([], ["Demo: safety baseline, graded by a decision model"]),
        })
        hot = AuditRun.objects.get(name="Demo: safety elevated-temp")
        # A setting the engine reads, so cloning / re-running really is hotter.
        self.assertEqual(hot.generation_parameters_snapshot["target_params"], {"temperature": 1.2})

    def test_older_seeds_are_grouped_on_the_next_boot(self):
        self._seed()
        Experiment.objects.all().delete()
        self._seed()   # runs exist: no new runs, just the experiments
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 5)
        self.assertEqual(len(self._experiments()), 3)
        self._seed()   # idempotent
        self.assertEqual(len(self._experiments()), 3)

    def test_force_replaces_demo_runs_and_their_experiments_only(self):
        self._seed()
        own = Experiment.objects.create(project=self.project, name="Mine", created_by=self.user)
        self._seed("--force")
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 5)
        self.assertEqual(Experiment.objects.filter(project=self.project).count(), 4)   # 3 demo + mine
        self.assertTrue(Experiment.objects.filter(pk=own.pk).exists())


DECISION_RUN = "Demo: safety baseline, graded by a decision model"


class DecisionDemoSeedTests(TestCase):
    """New users see a decision model at work: Jev registered, and a demo run it graded."""

    def setUp(self):
        self.user = UserFactory(is_superuser=True)
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        seed_workspace(self.project, self.user)
        seed_default_model_connections(self.project, self.user)

    def _seed(self, *args):
        call_command("seed_demo_audits", *args, project=self.project.id, stdout=StringIO(), stderr=StringIO())

    def test_jev_is_seeded_as_a_decision_model(self):
        from model_registry.models import ModelConnection, RegisteredModel

        conn = ModelConnection.objects.get(project=self.project, name="TypeSafe")
        self.assertEqual(conn.base_url, "https://api.typesafe.ai/v1")
        self.assertEqual(conn.secret_reference, "TYPESAFE_API_KEY")
        jev = RegisteredModel.objects.get(connection=conn, model_id="jev-latest")
        self.assertTrue(jev.is_decision)
        seed_default_model_connections(self.project, self.user)   # idempotent
        self.assertEqual(RegisteredModel.objects.filter(connection=conn).count(), 1)

    def test_the_decision_demo_run_is_graded_by_jev_with_a_decision_judge(self):
        from audits.events import ScenarioResult

        self._seed()
        run = AuditRun.objects.get(project=self.project, name=DECISION_RUN)
        self.assertEqual(run.judge_version.base, "decision_safety")
        self.assertEqual(run.judge_version.output, "decision")
        self.assertEqual(run.judge_model.display_name, "Jev (TypeSafe)")
        self.assertTrue(run.judge_config_snapshot["capabilities"]["decision"])
        # Same recorded transcripts as the chat-judged baseline: only the grading differs.
        self.assertEqual(run.target_model, AuditRun.objects.get(name="Demo: safety baseline").target_model)
        results = ScenarioResult.objects.filter(run_id=run.id)
        self.assertEqual(results.count(), 3)
        for result in results:
            self.assertTrue(result.result["judgment"]["judge_decision"]["model"].startswith("jev"))

    def test_a_workspace_seeded_before_the_decision_demo_gets_it_once(self):
        from accounts.models import SiteSetting

        self._seed()
        # As seeded by the previous release: no decision run, no record of given runs.
        AuditRun.objects.filter(name=DECISION_RUN).delete()
        SiteSetting.objects.filter(key__startswith="demo_audits_given:").delete()
        self._seed()
        self.assertTrue(AuditRun.objects.filter(name=DECISION_RUN).exists())
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 5)
        # Deleted afterwards: it stays deleted.
        AuditRun.objects.filter(name=DECISION_RUN).delete()
        self._seed()
        self.assertFalse(AuditRun.objects.filter(name=DECISION_RUN).exists())

    def test_a_deleted_demo_run_is_not_seeded_again(self):
        self._seed()
        AuditRun.objects.filter(name="Demo: rag baseline").delete()
        self._seed()
        self.assertFalse(AuditRun.objects.filter(name="Demo: rag baseline").exists())

    def test_the_decision_run_is_skipped_without_jev(self):
        from model_registry.models import RegisteredModel

        RegisteredModel.objects.filter(model_id="jev-latest").delete()
        self._seed()
        self.assertFalse(AuditRun.objects.filter(name=DECISION_RUN).exists())
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 4)


class PublicDemoExperimentsTests(TestCase):
    """Every seeded demo experiment is public, so a public deployment shows them without sign-in."""

    def setUp(self):
        self.user = UserFactory(is_superuser=True)
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        seed_workspace(self.project, self.user)
        seed_default_model_connections(self.project, self.user)

    def test_all_seeded_demo_experiments_are_public(self):
        call_command("seed_demo_audits", project=self.project.id, stdout=StringIO(), stderr=StringIO())
        experiments = Experiment.objects.filter(project=self.project)
        self.assertEqual(experiments.count(), 3)
        self.assertTrue(all(e.is_public for e in experiments))

    def test_migration_publishes_earlier_demo_experiments_only(self):
        from importlib import import_module

        from django.apps import apps

        from infra.tests.factories import AuditRunFactory

        call_command("seed_demo_audits", project=self.project.id, stdout=StringIO(), stderr=StringIO())
        Experiment.objects.update(visibility=Experiment.Visibility.PRIVATE)   # as seeded before
        # A demo experiment someone added their own run to stays private.
        mixed = Experiment.objects.get(name="Health vs RAG scenarios")
        mixed.runs.add(AuditRunFactory(project=self.project))
        # A user's own experiment that happens to share a demo name stays private.
        Experiment.objects.filter(name="Safety: graded by a decision model (Jev)").delete()
        mine = Experiment.objects.create(project=self.project, name="Safety: graded by a decision model (Jev)",
                                         created_by=self.user)
        mine.runs.add(AuditRunFactory(project=self.project))
        import_module("audits.migrations.0013_public_demo_experiments").make_demo_experiments_public(apps, None)
        self.assertTrue(Experiment.objects.get(name="Safety: does a hotter target change the verdicts?").is_public)
        self.assertFalse(Experiment.objects.get(pk=mixed.pk).is_public)
        self.assertFalse(Experiment.objects.get(pk=mine.pk).is_public)
