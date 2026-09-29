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
        })
        hot = AuditRun.objects.get(name="Demo: safety elevated-temp")
        # A setting the engine reads, so cloning / re-running really is hotter.
        self.assertEqual(hot.generation_parameters_snapshot["target_params"], {"temperature": 1.2})

    def test_older_seeds_are_grouped_on_the_next_boot(self):
        self._seed()
        AuditRun.objects.update(experiment=None)
        Experiment.objects.all().delete()
        self._seed()   # runs exist: no new runs, just the experiments
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 4)
        self.assertEqual(len(self._experiments()), 2)
        self._seed()   # idempotent
        self.assertEqual(len(self._experiments()), 2)

    def test_force_replaces_demo_runs_and_their_experiments_only(self):
        self._seed()
        own = Experiment.objects.create(project=self.project, name="Mine", created_by=self.user)
        self._seed("--force")
        self.assertEqual(AuditRun.objects.filter(project=self.project).count(), 4)
        self.assertEqual(Experiment.objects.filter(project=self.project).count(), 3)   # 2 demo + mine
        self.assertTrue(Experiment.objects.filter(pk=own.pk).exists())
