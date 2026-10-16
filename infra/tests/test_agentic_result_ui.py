"""Agentic results UI: evidence-linked findings, trajectory timeline, judge panel.

Covers the view helpers (``infra.ui._agentic_*_view``) and the rendered result
page blocks:
  (a) timeline rows render per span with unique anchors (duplicates suffixed),
  (b) finding cards are prioritized (severity then status) and jump to spans,
  (c) every block self-hides when its data is missing,
  (d) the current real payload shape renders without template errors.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_agentic_result_ui
"""
import json

from django.test import TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    ScenarioResultFactory,
    UserFactory,
)
from infra.ui import (
    _agentic_findings_view,
    _agentic_judge_view,
    _agentic_overview_view,
    _agentic_timeline_view,
    _rep_view,
)


def _span(span_id, name, kind="TOOL", offset_s=0.0, **attrs):
    start = 1_700_000_000.0 + offset_s
    return {
        "span_id": span_id,
        "name": name,
        "kind": kind,
        "start_time": start,
        "end_time": start + 0.02,
        "attributes": attrs,
    }


def _check(check_id, status, severity="low", summary="s", span_ids=None):
    return {
        "id": check_id,
        "category": "tool",
        "status": status,
        "severity": severity,
        "summary": summary,
        "expected": None,
        "observed": None,
        "evidence_span_ids": span_ids or [],
        "details": {},
    }


# The payload shape the worker attaches today (audits/agentic orchestrator).
REAL_AGENIC_EVALUATION = {
    "status": "FAIL",
    "reason": "1 check failed",
    "checks": [
        _check("trace.present", "PASS"),
        _check("tool.arguments", "FAIL", "high", "Wrong order id", ["f062125f30609f9f"]),
        _check("retrieval.required", "INCONCLUSIVE", "medium", "No retrieval evidence", []),
        _check("tool.required", "PASS", "critical", "Required tool called", ["f062125f30609f9f"]),
    ],
    "verdict": {"status": "FAIL", "reason": "tool.arguments failed"},
    "trajectory_stats": {"total_steps": 3, "tool_calls": 2, "errors": 0},
}

REAL_SPANS = [
    _span(
        "f062125f30609f9f", "acme_get_order", "TOOL", 0.05,
        **{
            "gen_ai.tool.name": "acme_get_order",
            "gen_ai.tool.call.arguments": '{"order_id": "ACME-9999"}',
            "gen_ai.tool.call.result": '{"error": "not found"}',
        },
    ),
    # Duplicate span id on purpose: repeated calls must anchor uniquely.
    _span("f062125f30609f9f", "acme_get_order", "TOOL", 0.10),
    _span(
        "397f73d050fb1b64", "query_knowledge_files", "RETRIEVER", 0.0,
        **{
            "gen_ai.tool.name": "query_knowledge_files",
            "openwebui.retrieval.data_source": ["Acme Retail Policy"],
            "gen_ai.tool.call.arguments": '{"query": "refund policy"}',
        },
    ),
]


class AgenticViewHelperTests(TestCase):
    def test_timeline_orders_chronologically_and_suffixes_duplicate_anchors(self):
        rep = {"judgment": {"evidence_spans": REAL_SPANS}}
        tl = _agentic_timeline_view(rep)
        self.assertEqual([r["span_id"] for r in tl["rows"]], [
            "397f73d050fb1b64", "f062125f30609f9f", "f062125f30609f9f",
        ])
        self.assertEqual([r["anchor"] for r in tl["rows"]], [
            "397f73d050fb1b64", "f062125f30609f9f", "f062125f30609f9f-2",
        ])
        # Jump links from a finding resolve to the first occurrence's anchor.
        self.assertEqual(tl["anchor_for"]["f062125f30609f9f"], "f062125f30609f9f")
        # KB sources surface on the retrieval row.
        self.assertEqual(tl["rows"][0]["kb_sources"], ["Acme Retail Policy"])
        self.assertTrue(tl["rows"][0]["is_retrieval"])

    def test_timeline_none_without_spans(self):
        self.assertIsNone(_agentic_timeline_view({}))
        self.assertIsNone(_agentic_timeline_view({"judgment": {}}))
        self.assertIsNone(_agentic_timeline_view({"judgment": {"evidence_spans": []}}))

    def test_findings_prioritized_severity_then_status(self):
        cards = _agentic_findings_view(REAL_AGENIC_EVALUATION)
        # Severity first (CRITICAL > HIGH > MEDIUM > LOW), then status within a
        # severity (FAIL > INCONCLUSIVE > ERROR > PASS).
        self.assertEqual([c["id"] for c in cards], [
            "tool.required",       # critical PASS — top severity wins over other rows' status
            "tool.arguments",      # high FAIL
            "retrieval.required",  # medium INCONCLUSIVE
            "trace.present",       # low PASS
        ])
        # Same-severity tie broken by status: FAIL before INCONCLUSIVE before PASS.
        tied = _agentic_findings_view({
            "checks": [
                _check("a.pass", "PASS"),
                _check("b.inconclusive", "INCONCLUSIVE"),
                _check("c.fail", "FAIL"),
            ]
        })
        self.assertEqual([c["id"] for c in tied], ["c.fail", "b.inconclusive", "a.pass"])

    def test_findings_empty_when_no_checks(self):
        self.assertEqual(_agentic_findings_view({}), [])
        self.assertEqual(_agentic_findings_view({"checks": "nope"}), [])

    def test_overview_reflects_verdict_and_counts(self):
        ov = _agentic_overview_view(REAL_AGENIC_EVALUATION)
        self.assertEqual(ov["status"], "FAIL")
        self.assertEqual((ov["n_passed"], ov["n_failed"], ov["n_inconclusive"]), (2, 1, 1))
        self.assertEqual(ov["first_failure"], "Wrong order id")
        self.assertIsNone(_agentic_overview_view({}))

    def test_judge_panel_from_target_contract(self):
        semantic = {
            "status": "INCONCLUSIVE",
            "rationale": "Not enough observation.",
            "model_version": "gpt-x v3",
            "prompt_version": "p9",
            "dimensions": {
                "grounding": {"status": "FAIL", "rationale": "Unsubstantiated claim", "span_ids": ["abc"]},
                "task_completion": {"status": "PASS", "rationale": "", "span_ids": []},
            },
        }
        judge = _agentic_judge_view({"semantic_judgement": semantic})
        self.assertEqual(judge["status"], "INCONCLUSIVE")
        self.assertEqual(len(judge["dimensions"]), 2)
        self.assertEqual(judge["dimensions"][0]["span_ids"], ["abc"])
        self.assertIsNone(_agentic_judge_view({}))
        self.assertIsNone(_agentic_judge_view({"semantic": {}}))


class AgenticResultPageRenderTests(TestCase):
    """The per-scenario result page renders the agentic blocks correctly."""

    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        from django.test import Client

        self.client = Client()
        self.client.login(username=self.user.username, password="pw")
        self.run = AuditRunFactory(project=self.project, status="completed")

    def _result(self, **overrides):
        rep = {
            "severity": "pass",
            "summary": "ok",
            "conversation": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
            "judgment": {"severity": "pass", "evidence_spans": REAL_SPANS},
            "trace_ids": ["d246dadb1e5c92fa91e9a2626c994c00"],
            "agentic_evaluation": json.loads(json.dumps(REAL_AGENIC_EVALUATION)),
        }
        rep.update(overrides)
        return rep

    def _get(self, rep):
        sr = ScenarioResultFactory(run_id=self.run.id, result=rep)
        return self.client.get(f"/runs/{self.run.id}/results/{sr.pk}/")

    def test_timeline_rows_render_with_unique_anchors(self):
        resp = self._get(self._result())
        self.assertContains(resp, 'id="trace-span-1-397f73d050fb1b64"')
        self.assertContains(resp, 'id="trace-span-1-f062125f30609f9f"')
        # The duplicate span id gets a -2 anchor so both rows exist.
        self.assertContains(resp, 'id="trace-span-1-f062125f30609f9f-2"')
        self.assertContains(resp, "Trajectory timeline")
        # KB source names are visible on the retrieval row.
        self.assertContains(resp, "Acme Retail Policy")

    def test_finding_cards_jump_to_evidence_spans(self):
        resp = self._get(self._result())
        # The failing check card cites its span via the resolved anchor.
        self.assertContains(resp, 'href="#trace-span-1-f062125f30609f9f"')
        self.assertContains(resp, "tool.arguments")
        self.assertContains(resp, "Agentic audit")

    def test_blocks_self_hide_without_agentic_data(self):
        resp = self._get({"severity": "pass", "summary": "plain", "conversation": []})
        for marker in ("Trajectory timeline", "Agentic audit", "Semantic judge", "Agentic check data"):
            self.assertNotContains(resp, marker)

    def test_judge_panel_hides_without_semantic_judgement(self):
        resp = self._get(self._result())
        self.assertNotContains(resp, "Semantic judge")
        # Add the target-contract semantic judgement -> panel appears.
        rep = self._result()
        rep["agentic_evaluation"]["semantic_judgement"] = {
            "status": "PASS",
            "rationale": "Grounded.",
            "model_version": "m1",
            "prompt_version": "p1",
            "dimensions": {"grounding": {"status": "PASS", "rationale": "ok", "span_ids": ["f062125f30609f9f"]}},
        }
        resp = self._get(rep)
        self.assertContains(resp, "Semantic judge")
        self.assertContains(resp, "Model: ")
        self.assertContains(resp, "Prompt: ")
        # Dimension citation jumps to the (first) evidence row.
        self.assertContains(resp, 'href="#trace-span-1-f062125f30609f9f"')

    def test_raw_details_collapsible(self):
        resp = self._get(self._result())
        self.assertContains(resp, "Agentic check data")
        self.assertContains(resp, "Captured spans (raw, 3)")

    def test_rep_view_exposes_agentic_sections(self):
        rep = self._result()
        view = _rep_view(rep, 1)
        self.assertIsNotNone(view["agentic_overview"])
        self.assertIsNotNone(view["agentic_timeline"])
        self.assertEqual(len(view["agentic_findings"]), 4)
        # Finding span ids resolved to anchors.
        self.assertEqual(view["agentic_findings"][0]["spans"][0]["anchor"], "f062125f30609f9f")
        self.assertIsNone(view["agentic_judge"])
        self.assertEqual(view["agentic_trace_completeness"]["span_count"], 3)
        self.assertIn("TOOL", view["agentic_trace_completeness"]["captured_kinds"])
