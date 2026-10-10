"""The New Experiment model picker marks decision-capable models.

Which models are decision models is settled on /connections/ (the probe writes
``capabilities.decision``); this is only the badge, so the picker renders from
``RegisteredModel.is_decision`` and nothing re-probes here.
"""

from __future__ import annotations

import pytest
from django.template.loader import render_to_string

ROLES = [
    ("target", "Target", "The model under audit", []),
    ("judge", "Judge", "Grades the answer", []),
]


class _Model:
    """A registered model as the picker sees it."""

    def __init__(self, pk, name, model_id, capabilities=None):
        self.id = self.pk = pk
        self.display_name = name
        self.model_id = model_id
        self.description = ""
        self.has_key = True
        self.capabilities = capabilities or {}

    @property
    def is_decision(self) -> bool:
        return bool((self.capabilities or {}).get("decision"))


class _Conn:
    def __init__(self, pk, name, models):
        self.pk = self.id = pk
        self.name = name
        self.description = ""
        self.model_list = models
        self.has_otlp = False
        self.is_shared = False
        self.share_label = "this workspace"
        self.project = type("P", (), {"name": "Workspace"})()


def _render(models):
    return render_to_string(
        "partials/model_pickers.html",
        {
            "connections": [_Conn(1, "Local Ollama", models)],
            "model_roles": ROLES,
            "sel": {},
            "agent_models": [],
        },
    )


DECISION = _Model(1, "Clef Flash (local)", "clef-flash", {"decision": True})
CHAT = _Model(2, "Qwen 3.5", "qwen3.5:2b")


def test_a_decision_model_is_badged():
    html = _render([DECISION])
    assert "Clef Flash (local)" in html
    assert ">decision</span>" in html


def test_a_chat_model_is_not_badged():
    html = _render([CHAT])
    assert "Qwen 3.5" in html
    assert ">decision</span>" not in html


def test_only_the_decision_model_is_badged_in_a_mixed_list():
    html = _render([DECISION, CHAT])
    assert html.count(">decision</span>") == len(ROLES), "one badge per role, for one model"


@pytest.mark.parametrize("capabilities", [{}, {"decision": False}, {"vision": True}, None])
def test_an_unprobed_or_negative_capability_gets_no_badge(capabilities):
    html = _render([_Model(3, "Some model", "some-model", capabilities)])
    assert ">decision</span>" not in html


def test_the_target_badge_says_a_run_is_one_turn():
    """The constraint a user needs before picking it as a target."""
    html = _render([DECISION])
    assert "single turn" in html
    # The judge/auditor roles get the other explanation.
    assert "cannot judge or drive an audit" in html
