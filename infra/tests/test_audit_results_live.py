"""Audit detail Results list: repetition display and the live-refresh fragment.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_audit_results_live
"""
from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    RepeatedScenarioResultFactory,
    ScenarioResultFactory,
    UserFactory,
)


class _ResultPagesBase(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")
        self.run = AuditRunFactory(
            project=self.project,
            status="judging",
            generation_parameters_snapshot={"n_repetitions": 3},
        )


class AuditResultsLiveTests(_ResultPagesBase):
    def _fragment(self):
        return self.client.get(f"/runs/{self.run.id}/results-fragment/")

    def test_fragment_grows_as_results_land(self):
        self.assertContains(self._fragment(), "Results (0)")
        RepeatedScenarioResultFactory(run_id=self.run.id)
        resp = self._fragment()
        self.assertContains(resp, "Results (1)")
        self.assertContains(resp, 'id="results-section"')

    def test_repetition_agreement_shown_as_percentage(self):
        RepeatedScenarioResultFactory(run_id=self.run.id)  # agreement_rate 0.6667
        resp = self._fragment()
        self.assertContains(resp, "3× · 67% agree")
        self.assertNotContains(resp, "1% agree")

    def test_repetition_summary_falls_back_to_matching_rep(self):
        RepeatedScenarioResultFactory(
            run_id=self.run.id,
            result={
                "reps": [
                    {"severity": "high", "summary": "Gave a diagnosis."},
                    {"severity": "pass", "summary": "Referred to a doctor."},
                    {"severity": "pass", "summary": "Declined politely."},
                ],
                "aggregated_severity": "pass",
                "agreement_rate": 0.6667,
                "severity_distribution": {"high": 1, "pass": 2},
                "n_repetitions": 3,
            },
        )
        resp = self._fragment()
        self.assertContains(resp, "Referred to a doctor.")
        self.assertContains(resp, "high ×1")

    def test_detail_page_includes_live_refresh(self):
        ScenarioResultFactory(run_id=self.run.id)
        page = self.client.get(f"/runs/{self.run.id}/")
        self.assertContains(page, 'id="results-section"')
        self.assertContains(page, f"/runs/{self.run.id}/results-fragment/")

    def test_fragment_is_project_scoped(self):
        other = AuditRunFactory()
        self.assertEqual(self.client.get(f"/runs/{other.id}/results-fragment/").status_code, 404)


class ScenarioResultPageTests(_ResultPagesBase):
    """The per-scenario result page shows every repetition's conversation."""

    def _rep(self, severity, text):
        return {
            "severity": severity,
            "summary": f"Summary {text}",
            "conversation": [
                {"role": "user", "content": f"Probe {text}"},
                {"role": "assistant", "content": f"Answer {text}"},
            ],
            "issues_found": ["hallucination"] if severity != "pass" else [],
            "positive_behaviors": [f"Good {text}"],
            "recommendations": [],
            "target_input_tokens": 10,
            "target_output_tokens": 20,
            "judgment": {"severity": severity},
        }

    def test_all_repetitions_are_shown_with_tabs(self):
        sr = RepeatedScenarioResultFactory(
            run_id=self.run.id,
            result={
                "reps": [self._rep("pass", "one"), self._rep("high", "two"), self._rep("pass", "three")],
                "aggregated_severity": "pass",
                "agreement_rate": 0.6667,
                "severity_distribution": {"pass": 2, "high": 1},
                "n_repetitions": 3,
            },
        )
        resp = self.client.get(f"/runs/{self.run.id}/results/{sr.pk}/")
        for text in ("one", "two", "three"):
            self.assertContains(resp, f"Answer {text}")
            self.assertContains(resp, f"Summary {text}")
        self.assertContains(resp, 'data-rep-tab="3"')
        self.assertContains(resp, "Auditor · turn 1")
        self.assertContains(resp, "Target · turn 1")
        self.assertContains(resp, "67% agree")
        # Opens on the dissenting repetition (rep 2).
        self.assertEqual(resp.context["initial_rep"], 2)
        self.assertEqual([r["dissent"] for r in resp.context["reps"]], [False, True, False])
        # judgment duplicates the named fields and is not dumped as "other".
        self.assertNotIn("judgment", resp.context["reps"][0]["other"])
        self.assertEqual(resp.context["reps"][0]["total_tokens"], 30)

    def test_single_rep_result_has_no_tabs(self):
        sr = ScenarioResultFactory(run_id=self.run.id, result=self._rep("pass", "solo"))
        resp = self.client.get(f"/runs/{self.run.id}/results/{sr.pk}/")
        self.assertContains(resp, "Answer solo")
        self.assertNotContains(resp, "data-rep-tab")
