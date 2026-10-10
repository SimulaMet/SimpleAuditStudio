"""Decision-capable models as audit targets: the gate, target construction, answer shaping.

No network. ``build_decision_target`` takes a ``factory`` so the real
``DecisionTarget`` is never constructed against a live endpoint.
"""

from __future__ import annotations

import pytest

from model_registry.decision import (
    build_decision_target,
    decision_answer,
    decision_target_for_model,
    decision_target_for_snapshot,
    is_decision_snapshot,
)


class _Conn:
    def __init__(self, provider="", base_url="", secret_reference=""):
        self.provider = provider
        self.base_url = base_url
        self.secret_reference = secret_reference

    def __str__(self):
        return f"conn({self.provider})"


class _Model:
    """Stands in for RegisteredModel, with the same ``is_decision`` semantics."""

    def __init__(self, connection, model_id="clef-flash", capabilities=None):
        self.connection = connection
        self.model_id = model_id
        self.capabilities = capabilities if capabilities is not None else {"decision": True}

    @property
    def is_decision(self) -> bool:
        return bool((self.capabilities or {}).get("decision"))

    def __str__(self):
        return f"model({self.model_id})"


class _Factory:
    """Stand-in for simpleaudit.DecisionTarget: records the call, builds nothing."""

    def __init__(self):
        self.calls = []

    def ollama(self, model, base_url=None, **kw):
        self.calls.append(("ollama", model, {"base_url": base_url, **kw}))
        return ("ollama", model, base_url)

    def openrouter(self, model, api_key=None, **kw):
        self.calls.append(("openrouter", model, {"api_key": api_key, **kw}))
        return ("openrouter", model, api_key)


def _snap(**over):
    snap = {
        "model_id": "clef-flash",
        "provider": "ollama",
        "base_url": "http://localhost:11434/v1",
        "capabilities": {"decision": True},
        "secret_reference": "",
    }
    snap.update(over)
    return snap


# --- the capability gate --------------------------------------------------

def test_a_probed_decision_snapshot_is_recognised():
    assert is_decision_snapshot(_snap())


@pytest.mark.parametrize("caps", [None, {}, {"decision": False}, {"vision": True}])
def test_a_chat_snapshot_is_not(caps):
    assert not is_decision_snapshot(_snap(capabilities=caps))


@pytest.mark.parametrize("snapshot", [None, "text", 7, []])
def test_malformed_snapshot_is_not_a_decision_snapshot(snapshot):
    assert not is_decision_snapshot(snapshot)


def test_a_chat_snapshot_builds_no_target():
    """The engine calls this on every run, so a chat run must get None, not an error."""
    assert decision_target_for_snapshot(_snap(capabilities={}), factory=_Factory()) is None


# --- target construction --------------------------------------------------

def test_ollama_target_gets_the_server_root_not_the_v1_base():
    """DecisionTarget.ollama appends /v1/systemone itself, so /v1 must come off first."""
    f = _Factory()
    decision_target_for_snapshot(_snap(), factory=f)
    assert f.calls[0][0] == "ollama"
    assert f.calls[0][1] == "clef-flash"
    assert f.calls[0][2]["base_url"] == "http://localhost:11434"


def test_a_trailing_slash_is_also_stripped():
    f = _Factory()
    decision_target_for_snapshot(_snap(base_url="http://localhost:11434/"), factory=f)
    assert f.calls[0][2]["base_url"] == "http://localhost:11434"


def test_a_systemone_server_needs_a_base_url():
    with pytest.raises(ValueError, match="base URL"):
        decision_target_for_snapshot(_snap(base_url=""), factory=_Factory())


def test_openrouter_target_gets_the_resolved_key():
    f = _Factory()
    decision_target_for_snapshot(
        _snap(provider="openrouter", model_id="typesafe/jev-1.13", secret_reference="OPENROUTER_API_KEY"),
        api_key="resolved-key", factory=f,
    )
    assert f.calls[0][0] == "openrouter"
    assert f.calls[0][1] == "typesafe/jev-1.13"
    assert f.calls[0][2]["api_key"] == "resolved-key"


def test_openrouter_without_a_key_says_what_to_set():
    with pytest.raises(ValueError, match="secret reference"):
        decision_target_for_snapshot(_snap(provider="openrouter"), api_key="", factory=_Factory())


@pytest.mark.parametrize("provider", ["vllm", "openai", "simulachat", ""])
def test_any_systemone_server_works_without_a_provider_allow_list(provider):
    """The gate is capabilities.decision, not a list of known providers.

    Detection probes every non-OpenRouter server at the same endpoint
    (f4e91d2), so a provider added upstream later needs no change here.
    """
    f = _Factory()
    build_decision_target(
        model_id="clef-flash", provider=provider, base_url="http://vllm.internal:8000/v1", factory=f,
    )
    assert f.calls[0][0] == "ollama"  # the factory that builds {root}/v1/systemone
    assert f.calls[0][2]["base_url"] == "http://vllm.internal:8000"


def test_a_key_is_forwarded_to_a_self_hosted_server_that_needs_one():
    f = _Factory()
    build_decision_target(
        model_id="clef-flash", provider="vllm", base_url="http://vllm.internal:8000/v1",
        api_key="resolved-key", factory=f,
    )
    assert f.calls[0][2]["api_key"] == "resolved-key"


def test_openrouter_matching_ignores_case_and_padding():
    f = _Factory()
    build_decision_target(
        model_id="typesafe/jev-1.13", provider=" OpenRouter ", api_key="k", factory=f,
    )
    assert f.calls[0][0] == "openrouter"


# --- the live-model gate --------------------------------------------------

def test_a_registered_decision_model_builds_its_target():
    f = _Factory()
    m = _Model(_Conn(provider="ollama", base_url="http://localhost:11434/v1"))
    decision_target_for_model(m, factory=f)
    assert f.calls[0][:2] == ("ollama", "clef-flash")


def test_a_model_not_marked_as_a_decision_model_is_refused():
    """is_decision is the gate: the /connections/ probe decides, not this module."""
    m = _Model(_Conn(provider="ollama", base_url="http://localhost:11434"), capabilities={})
    with pytest.raises(ValueError, match="not marked as a decision model"):
        decision_target_for_model(m, factory=_Factory())


# --- answer shaping -------------------------------------------------------

def test_answer_is_shaped_for_the_template():
    msg = {
        "role": "assistant",
        "content": "rights: patient rights",
        "decision": {
            "choice": "rights",
            "confidence": 0.82,
            "probabilities": {"info": 0.18, "rights": 0.82},
        },
    }
    out = decision_answer(msg)
    assert out["choice"] == "rights"
    assert out["confidence_percent"] == 82.0
    # Highest probability first, so the chosen option reads at the top.
    assert [o["option"] for o in out["options"]] == ["rights", "info"]
    assert out["options"][0]["percent"] == 82.0


def test_an_ordinary_reply_has_no_decision():
    assert decision_answer({"role": "assistant", "content": "Here is an answer."}) is None


@pytest.mark.parametrize("msg", [None, {}, "text", {"decision": {}}, {"decision": "x"}])
def test_malformed_input_is_none_not_an_error(msg):
    assert decision_answer(msg) is None


def test_missing_probabilities_still_gives_the_choice():
    out = decision_answer({"decision": {"choice": "a", "confidence": 0.5}})
    assert out["choice"] == "a" and out["options"] == []


def test_a_missing_confidence_is_not_rendered_as_zero():
    out = decision_answer({"decision": {"choice": "a", "probabilities": {"a": 1.0}}})
    assert out["confidence_percent"] is None
