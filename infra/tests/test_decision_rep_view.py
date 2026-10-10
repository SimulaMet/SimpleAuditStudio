"""The result page's Decision answer card.

A decision target (SimpleAudit ``DecisionTarget``) answers once with a chosen
option, a probability for every option and a calibrated confidence. The auditor
stores that on the assistant reply as ``decision``; ``_rep_view`` lifts it onto
the rep so ``partials/result_rep_panel.html`` can render it.

Scenarios without a decision block must be untouched, which is most of them.
"""
from django.template.loader import render_to_string
from django.test import TestCase

from infra.ui import _rep_view


def _rep(answer=None, content="rights: patient rights"):
    reply = {"role": "assistant", "content": content}
    if answer is not None:
        reply["decision"] = answer
    return {
        "severity": "pass",
        "conversation": [{"role": "user", "content": "Classify this question."}, reply],
    }


ANSWER = {
    "choice": "rights",
    "confidence": 0.82,
    "probabilities": {"info": 0.18, "rights": 0.82},
}


class DecisionRepViewTest(TestCase):
    def test_decision_answer_is_lifted_onto_the_rep(self):
        view = _rep_view(_rep(ANSWER), 1)
        self.assertEqual(view["decision"]["choice"], "rights")
        self.assertEqual(view["decision"]["confidence_percent"], 82.0)
        self.assertEqual([o["option"] for o in view["decision"]["options"]], ["rights", "info"])

    def test_no_decision_for_an_ordinary_conversation(self):
        self.assertIsNone(_rep_view(_rep(), 1)["decision"])

    def test_no_decision_for_an_empty_conversation(self):
        self.assertIsNone(_rep_view({"severity": "pass", "conversation": []}, 1)["decision"])

    def test_the_conversation_itself_is_unchanged(self):
        """The reply still renders as a normal turn; the card is additional."""
        view = _rep_view(_rep(ANSWER), 1)
        self.assertEqual(view["turns"], 1)
        self.assertEqual(view["conversation"][1]["speaker"], "Target")
        self.assertEqual(view["conversation"][1]["content"], "rights: patient rights")

    def test_last_answer_wins_when_several_replies_carry_one(self):
        rep = {
            "severity": "pass",
            "conversation": [
                {"role": "assistant", "content": "a", "decision": {"choice": "a"}},
                {"role": "assistant", "content": "b", "decision": {"choice": "b"}},
            ],
        }
        self.assertEqual(_rep_view(rep, 1)["decision"]["choice"], "b")


class DecisionPanelRenderTest(TestCase):
    """The partial renders the card, and stays silent without one."""

    def _html(self, rep):
        return render_to_string("partials/result_rep_panel.html", {"rep": _rep_view(rep, 1)})

    def test_card_shows_choice_confidence_and_every_option(self):
        html = self._html(_rep(ANSWER))
        self.assertIn("Decision answer", html)
        self.assertIn("rights", html)
        self.assertIn("confidence 82.0%", html)
        self.assertIn("82.0%", html)
        self.assertIn("18.0%", html)

    def test_no_card_without_a_decision_answer(self):
        self.assertNotIn("Decision answer", self._html(_rep()))

    def test_card_without_probabilities_still_shows_the_choice(self):
        html = self._html(_rep({"choice": "rights", "confidence": 0.5}))
        self.assertIn("Decision answer", html)
        self.assertIn("confidence 50.0%", html)
