"""Human labels: a reviewer's own verdict and comment on a judged transcript."""
from django.test import Client, TestCase

from audits.labels import labels_by_rep, origin_result, run_scale, set_label
from audits.models import HumanLabel
from infra.exceptions import StableAPIError
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    RepeatedScenarioResultFactory,
    ScenarioResultFactory,
    UserFactory,
)


class HumanLabelTestCase(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.reviewer = self._member("reviewer", "auditor")
        self.run = AuditRunFactory(project=self.project)
        self.result = ScenarioResultFactory(run_id=self.run.id, version_item_id="7")

    def _member(self, username, role):
        user = UserFactory(username=username)
        user.set_password("pw")
        user.save()
        MembershipFactory(user=user, project=self.project, role=role)
        return user

    def _label(self, **kwargs):
        defaults = {"result": self.result, "rep": 1, "user": self.reviewer, "verdict": "high", "comment": ""}
        return set_label(**{**defaults, **kwargs})

    def _pass_fail_rejudge(self, base="pass_fail", criteria=""):
        """A rejudge of ``self.run`` with a pass/fail judge, and its result for the same scenario."""
        judge = {"spec": {"base": base, "criteria": criteria}, "criteria": criteria or "The default criteria."}
        run = AuditRunFactory(
            project=self.project, judge_config_snapshot={"judge": judge},
            runtime_metadata={"rejudged_from": {"run_id": self.run.id, "name": self.run.name}},
        )
        return run, ScenarioResultFactory(run_id=run.id, version_item_id="7")


class SetLabelTests(HumanLabelTestCase):
    def test_reviewer_has_one_label_per_transcript_and_saving_again_replaces_it(self):
        self._label(verdict="high", comment="  Gives a dose without asking weight.  ")
        self._label(verdict="critical", comment="Second look.")

        label = HumanLabel.objects.get()
        self.assertEqual((label.verdict, label.comment, label.created_by), ("critical", "Second look.", self.reviewer))
        self.assertEqual(label.scale, "severity")

    def test_several_reviewers_can_label_the_same_transcript(self):
        self._label(verdict="high")
        self._label(user=self._member("second", "admin"), verdict="pass")

        verdicts = [label.verdict for label in labels_by_rep(self.result)[1]]
        self.assertEqual(verdicts, ["high", "pass"])

    def test_blank_verdict_removes_only_the_reviewers_own_label(self):
        other = self._member("second", "auditor")
        self._label()
        self._label(user=other, verdict="low")

        self.assertIsNone(self._label(verdict=""))

        self.assertEqual([label.created_by for label in HumanLabel.objects.all()], [other])

    def test_unsure_is_a_label(self):
        self.assertEqual(self._label(verdict="unsure").verdict, "unsure")

    def test_unknown_verdict_and_missing_repetition_are_refused(self):
        for kwargs in ({"verdict": "terrible"}, {"rep": 2}, {"rep": 0}):
            with self.assertRaises(StableAPIError):
                self._label(**kwargs)
        self.assertFalse(HumanLabel.objects.exists())

    def test_each_repetition_is_labelled_on_its_own(self):
        repeated = RepeatedScenarioResultFactory(run_id=self.run.id, version_item_id="8")
        self._label(result=repeated, rep=1, verdict="high")
        self._label(result=repeated, rep=3, verdict="pass")

        by_rep = labels_by_rep(repeated)
        self.assertEqual({rep: labels[0].verdict for rep, labels in by_rep.items()}, {1: "high", 3: "pass"})
        with self.assertRaises(StableAPIError):
            self._label(result=repeated, rep=4)

    def test_viewer_cannot_label(self):
        with self.assertRaises(StableAPIError):
            self._label(user=self._member("reader", "viewer"))
        self.assertFalse(HumanLabel.objects.exists())


class RejudgedRunLabelTests(HumanLabelTestCase):
    """A rejudged run grades the same transcripts, so it shares its source's labels."""

    def setUp(self):
        super().setUp()
        self.rejudged = AuditRunFactory(
            project=self.project, runtime_metadata={"rejudged_from": {"run_id": self.run.id, "name": self.run.name}},
        )
        self.regraded = ScenarioResultFactory(run_id=self.rejudged.id, version_item_id="7")

    def test_label_given_on_a_rejudged_result_is_kept_on_the_source_result(self):
        self._label(result=self.regraded, verdict="medium")

        self.assertEqual(origin_result(self.regraded), self.result)
        self.assertEqual(HumanLabel.objects.get().result, self.result)
        self.assertEqual([label.verdict for label in labels_by_rep(self.result)[1]], ["medium"])
        self.assertEqual([label.verdict for label in labels_by_rep(self.regraded)[1]], ["medium"])

    def test_rejudge_of_a_rejudge_leads_back_to_the_first_run(self):
        second = AuditRunFactory(
            project=self.project, runtime_metadata={"rejudged_from": {"run_id": self.rejudged.id, "name": "r"}},
        )
        again = ScenarioResultFactory(run_id=second.id, version_item_id="7")
        self.assertEqual(origin_result(again), self.result)

    def test_result_the_source_does_not_have_is_its_own_origin(self):
        alone = ScenarioResultFactory(run_id=self.rejudged.id, version_item_id="99")
        self.assertEqual(origin_result(alone), alone)


class LabelScaleTests(HumanLabelTestCase):
    """A label is on the scale of the judge that graded the run it is given in."""

    def test_run_of_a_pass_fail_judge_is_labelled_pass_or_fail(self):
        _, regraded = self._pass_fail_rejudge()

        label = self._label(result=regraded, verdict="fail")

        self.assertEqual((label.scale, label.verdict, label.result), ("pass_fail", "fail", self.result))
        for verdict in ("high", "medium"):
            with self.assertRaises(StableAPIError):
                self._label(result=regraded, verdict=verdict)
        self.assertEqual(self._label(result=regraded, verdict="unsure").verdict, "unsure")

    def test_three_level_judge_takes_medium_and_the_ladder_takes_no_fail(self):
        _, regraded = self._pass_fail_rejudge(base="pass_medium_fail")

        self.assertEqual(self._label(result=regraded, verdict="medium").scale, "pass_medium_fail")
        with self.assertRaises(StableAPIError):
            self._label(verdict="fail")

    def test_label_on_another_scale_is_kept_beside_the_first(self):
        _, regraded = self._pass_fail_rejudge()
        self._label(verdict="high", comment="Ladder label.")
        self._label(result=regraded, verdict="fail")

        self.assertIsNone(self._label(result=regraded, verdict=""))

        self.assertEqual([(label.scale, label.verdict) for label in HumanLabel.objects.all()], [("severity", "high")])

    def test_rules_shown_are_the_judges_own_criteria_and_levels(self):
        run, _ = self._pass_fail_rejudge(criteria="Only check the dose.")

        rubric = run_scale(run)["rubric"]

        self.assertIn("Only check the dose.", rubric)
        self.assertIn("VERDICT (pass or fail)", rubric)
        self.assertEqual(run_scale(self.run), {
            "name": "severity", "levels": ["pass", "low", "medium", "high", "critical"], "rubric": None,
        })


class HumanLabelPageTests(HumanLabelTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="reviewer", password="pw")
        self.page = f"/runs/{self.run.id}/results/{self.result.id}/"
        self.url = f"{self.page}label/"

    def test_reviewer_saves_a_label_from_the_result_page(self):
        self.assertIn(f'action="{self.url}"', self.client.get(self.page).content.decode())

        response = self.client.post(self.url, {"rep": "1", "verdict": "high", "comment": "Unsafe dose advice."})

        self.assertRedirects(response, f"{self.page}#rep-1", fetch_redirect_response=False)
        label = HumanLabel.objects.get()
        self.assertEqual((label.verdict, label.comment, label.created_by), ("high", "Unsafe dose advice.", self.reviewer))
        html = self.client.get(self.page).content.decode()
        self.assertIn("Unsafe dose advice.", html)
        self.assertIn('<option value="high" selected>', html)

    def test_pass_fail_run_offers_its_levels_and_rules_and_lists_ladder_labels_with_their_scale(self):
        run, regraded = self._pass_fail_rejudge()
        self._label(verdict="high", comment="Ladder label.")
        page = f"/runs/{run.id}/results/{regraded.id}/"

        html = self.client.get(page).content.decode()

        self.assertIn('<option value="fail">Fail</option>', html)
        self.assertNotIn('<option value="critical"', html)
        self.assertIn("VERDICT (pass or fail)", html)
        self.assertIn("on the severity scale", html)

        self.client.post(f"{page}label/", {"rep": "1", "verdict": "fail"})
        self.assertIn('<option value="fail" selected>', self.client.get(page).content.decode())
        self.assertNotIn("Rules for this label", self.client.get(self.page).content.decode())

    def test_invalid_label_is_reported_and_not_saved(self):
        response = self.client.post(self.url, {"rep": "1", "verdict": "terrible"}, follow=True)
        self.assertContains(response, "Label must be one of")
        self.assertFalse(HumanLabel.objects.exists())

    def test_viewer_reads_labels_but_gets_no_form(self):
        self._label(verdict="low", comment="Fine, slightly vague.")
        self._member("reader", "viewer")
        reader = Client()
        reader.login(username="reader", password="pw")

        html = reader.get(self.page).content.decode()
        self.assertIn("Fine, slightly vague.", html)
        self.assertNotIn(self.url, html)
        reader.post(self.url, {"rep": "1", "verdict": "critical"})
        self.assertEqual(HumanLabel.objects.get().verdict, "low")

    def test_labels_are_not_shown_on_a_public_result_page(self):
        from audits.models import Experiment, ExperimentRun

        experiment = Experiment.objects.create(
            project=self.project, name="Public", visibility=Experiment.Visibility.PUBLIC,
        )
        ExperimentRun.objects.create(experiment=experiment, run=self.run)
        self._label(verdict="high", comment="Internal reviewer note.")

        response = Client().get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Internal reviewer note.")
        self.assertNotContains(response, "Human label")

    def test_result_of_another_workspace_cannot_be_labelled(self):
        foreign = ScenarioResultFactory(run_id=AuditRunFactory().id, version_item_id="7")
        response = self.client.post(f"/runs/{foreign.run_id}/results/{foreign.id}/label/", {"rep": "1", "verdict": "high"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(HumanLabel.objects.exists())
