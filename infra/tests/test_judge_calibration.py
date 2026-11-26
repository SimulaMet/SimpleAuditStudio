"""Judge calibration: how the judges tried on the same transcripts agree with human labels."""
from django.test import Client, TestCase

from audits.labels import calibration, set_label
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    RepeatedScenarioResultFactory,
    ScenarioResultFactory,
    UserFactory,
)


def _result(run, item_id, severity, verdict=None, scale="pass_fail"):
    """A stored result; with ``verdict``, one a pass/fail judge graded (severity derived from it)."""
    judgment = {"verdict": verdict, "verdict_scale": scale, "severity": severity} if verdict else {"severity": severity}
    return ScenarioResultFactory(
        run_id=run.id, version_item_id=str(item_id),
        result={
            "severity": severity, "summary": "", "conversation": [{"role": "user", "content": "q"}],
            "judgment": judgment,
        },
    )


class CalibrationTestCase(TestCase):
    """A run of three scenarios (judge: pass, high, low) and a rejudge of it (pass, medium, pass)."""

    def setUp(self):
        self.project = ProjectFactory()
        self.first = self._member("first", "auditor")
        self.second = self._member("second", "admin")
        self.run = AuditRunFactory(project=self.project, name="Source")
        self.results = [_result(self.run, item, sev) for item, sev in ((1, "pass"), (2, "high"), (3, "low"))]
        self.rejudged = AuditRunFactory(
            project=self.project, name="Rejudged",
            runtime_metadata={"rejudged_from": {"run_id": self.run.id, "name": "Source"}},
        )
        self.regraded = [_result(self.rejudged, item, sev) for item, sev in ((1, "pass"), (2, "medium"), (3, "pass"))]

    def _member(self, username, role):
        user = UserFactory(username=username)
        user.set_password("pw")
        user.save()
        MembershipFactory(user=user, project=self.project, role=role)
        return user

    def _label(self, result, verdict, user=None, rep=1):
        return set_label(result=result, rep=rep, user=user or self.first, verdict=verdict)

    def _verdict_rejudge(self, verdicts, base="pass_fail"):
        """A rejudge of ``self.run`` by a pass/fail judge: one verdict per scenario, a fail stored as critical."""
        run = AuditRunFactory(
            project=self.project, name=base, judge_config_snapshot={"judge": {"spec": {"base": base}}},
            runtime_metadata={"rejudged_from": {"run_id": self.run.id, "name": "Source"}},
        )
        severity = {"pass": "pass", "medium": "medium", "fail": "critical"}
        return run, [
            _result(run, item, severity[verdict], verdict, base) for item, verdict in enumerate(verdicts, start=1)
        ]


class CalibrationTests(CalibrationTestCase):
    def test_nothing_labelled_gives_no_report(self):
        self.assertIsNone(calibration(self.run))

    def test_each_judge_tried_on_the_transcripts_is_scored_against_the_same_labels(self):
        for result, severity in zip(self.results, ("pass", "medium", "low"), strict=True):
            self._label(result, severity)

        report = calibration(self.run)

        self.assertEqual([row["run"] for row in report["rows"]], [self.run, self.rejudged])
        source, rejudged = report["rows"]
        # Source judge: pass, high, low against pass, medium, low.
        self.assertEqual((source["exact"]["k"], source["exact"]["n"]), (2, 3))
        self.assertEqual((source["bias"]["stricter"], source["bias"]["lenient"]), (1, 0))
        # Rejudge: pass, medium, pass against the same labels.
        self.assertEqual((rejudged["exact"]["k"], rejudged["exact"]["n"]), (2, 3))
        self.assertEqual((rejudged["bias"]["stricter"], rejudged["bias"]["lenient"]), (0, 1))
        self.assertEqual(rejudged["disagreements"], [{"scenario": "3#1", "reference": "low", "judge": "pass", "gap": -1}])
        self.assertEqual((len(report["reference"]["raters"]), report["reference"]["n_items"]), (1, 3))
        self.assertEqual((report["scale"], report["unreadable"]), ("severity", 0))
        # Asked from the rejudged run, the report is the same.
        self.assertEqual([row["run"] for row in calibration(self.rejudged)["rows"]], [self.run, self.rejudged])

    def test_labels_given_from_a_rejudged_result_count_for_every_run(self):
        self._label(self.regraded[1], "high")

        source, rejudged = calibration(self.run)["rows"]
        self.assertEqual((source["exact"]["k"], source["exact"]["n"]), (1, 1))      # judge said high
        self.assertEqual((rejudged["exact"]["k"], rejudged["exact"]["n"]), (0, 1))  # judge said medium

    def test_several_reviewers_give_a_median_reference_and_unsure_is_left_out(self):
        self._label(self.results[1], "high", user=self.first)
        self._label(self.results[1], "low", user=self.second)       # split: stricter middle is high
        self._label(self.results[0], "unsure", user=self.second)    # not a label to compare against

        report = calibration(self.run)

        self.assertEqual(len(report["reference"]["raters"]), 2)
        self.assertEqual(report["reference"]["consensus"], {"2#1": "high"})
        self.assertEqual(report["reference"]["n_split"], 1)
        self.assertEqual(report["rows"][0]["exact"]["k"], 1)

    def test_repeated_scenario_is_compared_per_repetition(self):
        repeated = RepeatedScenarioResultFactory(run_id=self.run.id, version_item_id="4")   # high, pass, pass
        self._label(repeated, "high", rep=1)
        self._label(repeated, "high", rep=2)

        source = calibration(self.run)["rows"][0]
        self.assertEqual((source["exact"]["k"], source["exact"]["n"]), (1, 2))
        self.assertEqual(source["disagreements"][0]["scenario"], "4#2")

    def test_failed_scenario_counts_as_not_graded(self):
        failed = ScenarioResultFactory(run_id=self.run.id, version_item_id="5", status="failed", result={"error": "boom"})
        self._label(failed, "high")

        coverage = calibration(self.run)["rows"][0]["coverage"]
        self.assertEqual((coverage["reference"], coverage["compared"], coverage["error"]), (1, 0, 1))

    def test_run_of_another_workspace_is_never_in_the_report(self):
        self._label(self.results[0], "pass")
        foreign = AuditRunFactory(runtime_metadata={"rejudged_from": {"run_id": self.run.id, "name": "x"}})
        _result(foreign, 1, "critical")

        self.assertNotIn(foreign, [row["run"] for row in calibration(self.run)["rows"]])


class CalibrationScaleTests(CalibrationTestCase):
    """Judges and labels are compared on the scale of the judge whose run is asked about."""

    def test_pass_fail_run_compares_every_judge_and_ladder_labels_as_pass_or_fail(self):
        run, _ = self._verdict_rejudge(("pass", "fail", "pass"))
        for result, severity in zip(self.results, ("pass", "medium", "low"), strict=True):
            self._label(result, severity)       # on the ladder: read as pass, fail, fail

        report = calibration(run)

        self.assertEqual((report["scale"], report["levels"]), ("pass_fail", ["pass", "fail"]))
        self.assertEqual(report["reference"]["consensus"], {"1#1": "pass", "2#1": "fail", "3#1": "fail"})
        source, _rejudged, verdict_judge = report["rows"]
        # The source judge said pass, high, low: read as pass, fail, fail.
        self.assertEqual((source["exact"]["k"], source["exact"]["n"]), (3, 3))
        self.assertEqual((source["scale"], verdict_judge["scale"]), ("severity", "pass_fail"))
        # The pass/fail judge is read by its verdict (pass, fail, pass), not its severity.
        self.assertEqual(verdict_judge["disagreements"], [{"scenario": "3#1", "reference": "fail", "judge": "pass", "gap": -1}])

    def test_label_given_on_the_scale_wins_over_one_read_from_the_ladder(self):
        run, regraded = self._verdict_rejudge(("pass", "fail", "pass"))
        self._label(self.results[0], "low")     # read as fail
        self._label(regraded[0], "pass")        # the same reviewer, on pass/fail

        report = calibration(run)

        self.assertEqual(report["reference"]["consensus"], {"1#1": "pass"})
        self.assertEqual(len(report["reference"]["raters"]), 1)

    def test_ladder_run_leaves_out_pass_fail_labels_and_scores_the_verdict_judge_by_its_severity(self):
        _, regraded = self._verdict_rejudge(("pass", "fail", "pass"))
        self._label(self.results[1], "medium")
        self._label(regraded[2], "fail")        # pass/fail only: no reading on the ladder

        report = calibration(self.run)

        self.assertEqual((report["scale"], report["unreadable"]), ("severity", 1))
        self.assertEqual(report["reference"]["consensus"], {"2#1": "medium"})
        # A fail is stored as the scenario's own severity, here critical.
        self.assertEqual(report["rows"][2]["disagreements"][0]["judge"], "critical")

    def test_no_label_readable_on_the_scale_gives_rows_without_agreement(self):
        _, regraded = self._verdict_rejudge(("pass", "fail", "pass"))
        self._label(regraded[1], "fail")

        report = calibration(self.run)

        self.assertIsNone(report["reference"])
        self.assertEqual(report["unreadable"], 1)
        self.assertTrue(all("coverage" not in row for row in report["rows"]))

    def test_judge_on_a_scale_the_page_cannot_read_gets_a_row_without_agreement(self):
        self._verdict_rejudge(("pass", "fail", "pass"))
        three, regraded = self._verdict_rejudge(("pass", "medium", "fail"), base="pass_medium_fail")
        self._label(regraded[1], "medium")

        _source, _rejudged, two_level, three_level = calibration(three)["rows"]

        self.assertNotIn("coverage", two_level)     # pass/fail verdicts say nothing about medium
        self.assertEqual((three_level["exact"]["k"], three_level["exact"]["n"]), (1, 1))

    def test_list_summary_keeps_the_verdict_for_single_and_repeated_results(self):
        _, regraded = self._verdict_rejudge(("pass", "fail", "pass"))
        self.assertEqual(
            (regraded[1].list_summary["verdict"], regraded[1].list_summary["verdict_scale"]), ("fail", "pass_fail"),
        )
        self.assertNotIn("verdict", self.results[0].list_summary)
        rep = {"severity": "high", "conversation": [], "judgment": {"verdict": "fail", "verdict_scale": "pass_fail"}}
        repeated = ScenarioResultFactory(run_id=self.run.id, version_item_id="9", result={"reps": [rep, rep]})
        self.assertEqual([trial["verdict"] for trial in repeated.list_summary["reps"]], ["fail", "fail"])


class CalibrationPageTests(CalibrationTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="first", password="pw")
        self.url = f"/runs/{self.rejudged.id}/calibration/"

    def test_page_without_labels_says_how_to_start(self):
        response = self.client.get(self.url)
        self.assertContains(response, "No transcript of this run has a human label yet")

    def test_page_lists_every_judge_and_links_this_runs_disagreements(self):
        for result, severity in zip(self.results, ("pass", "medium", "low"), strict=True):
            self._label(result, severity)

        html = self.client.get(self.url).content.decode()

        self.assertIn(f'href="/runs/{self.run.id}/"', html)
        self.assertIn(f'href="/runs/{self.rejudged.id}/"', html)
        self.assertIn("3 labelled transcripts from 1 reviewer", html)
        self.assertIn("2/3", html)
        # The rejudged run's judge said pass where the reviewer said low: linked to that result.
        self.assertIn(f'href="/runs/{self.rejudged.id}/results/{self.regraded[2].id}/#rep-1"', html)
        self.assertNotIn(f'/results/{self.regraded[0].id}/', html)
        self.assertIn("indicative only", html)

    def test_pass_fail_page_names_its_scale_and_leaves_out_the_ordinal_columns(self):
        run, regraded = self._verdict_rejudge(("pass", "fail", "pass"))
        self._label(regraded[2], "fail")
        self._label(self.results[1], "high")

        html = self.client.get(f"/runs/{run.id}/calibration/").content.decode()

        self.assertIn("Compared on the pass_fail scale (pass, fail)", html)
        self.assertIn(">AC1</th>", html)
        self.assertNotIn("Within one level", html)
        self.assertNotIn("Severe misses", html)
        # The severity judges are marked as graded on another scale, with a link to their own calibration.
        self.assertIn(f'href="/runs/{self.run.id}/calibration/"', html)
        self.assertIn("A severity is read as a level of this scale", html)

        ladder = self.client.get(f"/runs/{self.run.id}/calibration/").content.decode()
        self.assertIn("1 label given on another scale cannot be read on this one and is left out", ladder)
        self.assertIn("Within one level", ladder)
        self.assertIn("scored here by the severity derived from its verdict", ladder)

    def test_page_says_when_no_label_can_be_read_on_the_runs_scale(self):
        _, regraded = self._verdict_rejudge(("pass", "fail", "pass"))
        self._label(regraded[1], "fail")

        response = self.client.get(f"/runs/{self.run.id}/calibration/")

        self.assertContains(response, "cannot be read on the severity scale")

    def test_run_page_links_to_calibration(self):
        self.assertContains(self.client.get(f"/runs/{self.run.id}/"), f'href="/runs/{self.run.id}/calibration/"')

    def test_viewer_can_read_the_page(self):
        self._label(self.results[0], "pass")
        self._member("reader", "viewer")
        reader = Client()
        reader.login(username="reader", password="pw")
        self.assertContains(reader.get(self.url), "Judge calibration")

    def test_run_of_another_workspace_is_not_found(self):
        self.assertEqual(self.client.get(f"/runs/{AuditRunFactory().id}/calibration/").status_code, 404)
