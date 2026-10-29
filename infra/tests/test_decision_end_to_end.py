"""End-to-end: a decision-capable registered model answers, and run detail shows it.

The CI test replays ``infra/fixtures/decision_clef_flash.json`` — the actual
reply recorded from clef-flash (9.1B, Q8_0) on a local Ollama — through an
``httpx.MockTransport``, so the whole chain is exercised without a model:
frozen snapshot → ``DecisionTarget`` → engine run → ``_rep_view`` → template.

``test_live_clef_flash_answers_and_renders`` is the same path against the real
server; it skips unless Ollama is serving clef-flash.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest
from django.template.loader import render_to_string

from infra.engine import decision_target_for, install_target, without_target_client
from infra.ui import _rep_view

FIXTURE = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "decision_clef_flash.json"

#: What ``audits.services._endpoint_snapshot`` freezes for a decision-capable
#: model: provider and base URL from the connection, the capability flag the
#: /connections/ probe wrote onto ``RegisteredModel.capabilities``.
SNAPSHOT = {
    "id": 1,
    "connection_id": 1,
    "display_name": "Clef Flash (local)",
    "provider": "ollama",
    "base_url": "http://localhost:11434/v1",
    "model_id": "clef-flash",
    "model_revision": "",
    "capabilities": {"decision": True},
    "default_parameters": {},
    "secret_reference": "",
    "enabled": True,
}

SCENARIO = {
    "name": "Right of access to one's own medical record",
    "description": "A patient asks a hospital chatbot for a copy of their own medical record.",
    "decision": {
        "id": "verdict",
        "type": "choice",
        "instructions": (
            "Does the Norwegian Patient Rights Act give the patient a right of access "
            "to their own medical record?"
        ),
        "criteria": {
            "yes": "Yes, a right of access applies",
            "no": "No right of access",
            "unclear": "Cannot be determined from the Act",
        },
        "accepted": ["yes"],
    },
}


def _recorded_reply() -> dict:
    return json.loads(FIXTURE.read_text())


def _clef_flash_is_served() -> bool:
    """Whether a local Ollama lists clef-flash. Spends no inference."""
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        models = resp.json().get("models") or []
    except Exception:  # noqa: BLE001 - no server, no live test
        return False
    return any("clef-flash" in (m.get("name") or m.get("model") or "") for m in models)


def _run(target) -> dict:
    """One decision scenario through the engine, as a run does. Returns the rep dict."""
    from simpleaudit.model_auditor import ModelAuditor

    # choice_match grades in code, so a decision run needs no judge or auditor model.
    auditor = without_target_client(ModelAuditor)(
        model=SNAPSHOT["model_id"], provider="ollama", base_url=SNAPSHOT["base_url"],
        api_key="no-auth", judge_model="unused", judge_provider="openai",
        judge="choice_match", max_turns=1, json_format=True,
        show_progress=False, verbose=False,
    )
    assert install_target(auditor, target) is True
    results = auditor.run(scenarios=[SCENARIO], language="English")
    return results[0].to_dict()


def _assert_run_detail_shows_the_answer(rep: dict, recorded: dict) -> dict:
    """The decision answer survives into run detail and renders in the panel."""
    view = _rep_view(rep, 1)
    answer = recorded["answers"]["verdict"]

    assert view["decision"] is not None, "run detail carries no decision answer"
    assert view["decision"]["choice"] == answer["choice"]
    assert view["decision"]["confidence"] == pytest.approx(answer["confidence"])
    # Every option the model scored, highest first.
    assert {o["option"] for o in view["decision"]["options"]} == set(answer["probabilities"])
    percents = [o["percent"] for o in view["decision"]["options"]]
    assert percents == sorted(percents, reverse=True)

    html = render_to_string("partials/result_rep_panel.html", {"rep": view})
    assert "Decision answer" in html
    assert answer["choice"] in html
    assert f"confidence {view['decision']['confidence_percent']}%" in html
    for option in answer["probabilities"]:
        assert option in html
    return view


@pytest.mark.django_db
def test_recorded_clef_flash_reply_reaches_run_detail():
    """The engine's decision answer is surfaced; HTTP is mocked, nothing else is."""
    recorded = _recorded_reply()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=recorded)

    target = decision_target_for(SNAPSHOT)
    assert target.url == "http://localhost:11434/v1/systemone"
    target._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    rep = _run(target)
    view = _assert_run_detail_shows_the_answer(rep, recorded)

    # The request the engine actually sent: the model and the scenario's options.
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["model"] == "clef-flash"
    assert set(body["questions"]["verdict"]["criteria"]) == set(SCENARIO["decision"]["criteria"])
    assert view["decision"]["choice"] in SCENARIO["decision"]["criteria"]


@pytest.mark.django_db
def test_a_chat_model_still_gets_the_trace_adapter():
    """The decision path must not capture ordinary runs."""
    assert decision_target_for({**SNAPSHOT, "capabilities": {}}) is None


@pytest.mark.slow
@pytest.mark.django_db
@pytest.mark.skipif(not _clef_flash_is_served(), reason="no local Ollama serving clef-flash")
def test_live_clef_flash_answers_and_renders():
    """The same path against the real model: it must answer with one of the options."""
    target = decision_target_for(SNAPSHOT)
    rep = _run(target)
    view = _rep_view(rep, 1)

    assert view["decision"] is not None
    assert view["decision"]["choice"] in SCENARIO["decision"]["criteria"]
    assert 0.0 <= view["decision"]["confidence"] <= 1.0
    probabilities = [o["probability"] for o in view["decision"]["options"]]
    assert sum(probabilities) == pytest.approx(1.0, abs=0.01)

    html = render_to_string("partials/result_rep_panel.html", {"rep": view})
    assert "Decision answer" in html and view["decision"]["choice"] in html


# ─── max_turns and trace context (issue #17, item 2) ─────────────────────────

def _kwargs_for(snapshot, generation):
    from infra.engine import auditor_kwargs

    other = {"model_id": "gpt-4o", "provider": "openai", "base_url": "", "secret_reference": ""}
    kwargs, _ = auditor_kwargs(
        target=snapshot, auditor=other, judge=other, generation=generation,
        resolve_key=lambda snap: "key",
    )
    return kwargs


def test_max_turns_is_forced_to_one_for_a_decision_target():
    """DecisionTarget.max_turns is 1: a follow-up turn has nothing to send."""
    assert _kwargs_for(SNAPSHOT, {"max_turns": 5})["max_turns"] == 1


def test_max_turns_from_the_generation_config_still_applies_to_chat_targets():
    chat = {**SNAPSHOT, "capabilities": {}}
    assert _kwargs_for(chat, {"max_turns": 5})["max_turns"] == 5


def test_choice_match_judge_is_forced_for_a_decision_target():
    """A decision run is graded in code; no LLM judge is invoked."""
    kwargs = _kwargs_for(SNAPSHOT, {"max_turns": 1})
    assert kwargs["judge"] == "choice_match"
    assert kwargs["probe_prompt"] is None


def test_llm_judge_is_still_used_for_chat_targets():
    """Non-decision targets keep their frozen LLM judge untouched."""
    chat = {**SNAPSHOT, "capabilities": {}}
    kwargs = _kwargs_for(chat, {"max_turns": 3})
    assert kwargs["judge"] != "choice_match"


def test_scenario_dict_includes_decision_block():
    """The engine's scenario dict carries the decision block through."""
    from infra.engine import scenario_dict

    d = scenario_dict(
        name="dec", description="Pick A or B",
        expected_behavior=None, test_prompt=None,
        decision={"id": "d1", "type": "choice", "instructions": "Pick one.",
                  "criteria": {"A": "Option A", "B": "Option B"},
                  "accepted": ["A"]},
    )
    assert d["decision"]["accepted"] == ["A"]
    # A non-decision scenario has no decision key
    d2 = scenario_dict(name="chat", description="Say hi")
    assert "decision" not in d2


def test_validate_decision_block_accepts_valid_and_rejects_invalid():
    from infra.engine import validate_decision_block

    # Valid block passes silently
    validate_decision_block({"id": "d1", "type": "choice", "instructions": "x",
                             "criteria": {"A": "Option A", "B": "Option B"},
                             "accepted": ["A"]})
    # Missing criteria raises ValueError
    with pytest.raises(ValueError):
        validate_decision_block({"id": "d1", "type": "choice", "instructions": "x"})
    # accepted key not in criteria raises ValueError
    with pytest.raises(ValueError):
        validate_decision_block({"id": "d1", "type": "choice", "instructions": "x",
                                 "criteria": {"A": "Option A", "B": "Option B"},
                                 "accepted": ["C"]})
    # None / empty is a no-op
    validate_decision_block(None)
    validate_decision_block({})


@pytest.mark.django_db
def test_a_decision_target_forwards_the_per_turn_traceparent():
    """Why the trace-context adapter is not installed for decision runs.

    ``install_trace_context_target`` wraps the OpenAI-compatible chat client
    in a ModelTarget; applying it to a decision run would replace the
    DecisionTarget. It is not needed: DecisionTarget merges
    ``TargetContext.trace_headers`` into its own request headers, so per-turn
    traceparent propagation is not lost.
    """
    import asyncio

    from simpleaudit.targets.base import TargetContext

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_recorded_reply())

    target = decision_target_for(SNAPSHOT)
    target._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    asyncio.run(
        target.send(
            user=SCENARIO["description"],
            context=TargetContext(
                audit_run_id="audit_1", trace_headers={"traceparent": traceparent},
                extra={"decision": SCENARIO["decision"]},
            ),
        )
    )
    assert seen[0].headers.get("traceparent") == traceparent


@pytest.mark.django_db
def test_a_decision_auditor_built_per_repetition_has_its_clients_closed():
    """The repetition runner's decision auditors join the engine's client cleanup.

    ``run_with_client_cleanup`` closes the async clients of every auditor built
    while it runs, before ``asyncio.run()`` shuts the loop that owns them.
    ``TraceContextModelAuditor`` registers itself for that; a decision run's
    per-repetition auditor has to as well, or its judge client is left for GC
    to finalize on a closed loop.
    """
    import asyncio

    from infra.trace_target import (
        _collect_async_clients,
        decision_auditor_class,
        run_with_client_cleanup,
    )

    # Resolved before the loop: snapshot_api_key reads the connection from the
    # database, which Django refuses inside a running event loop.
    target = decision_target_for(SNAPSHOT)
    auditor_cls = decision_auditor_class(lambda: target)
    built = []

    async def one_repetition():
        built.append(auditor_cls(
            model=SNAPSHOT["model_id"], provider="ollama", base_url=SNAPSHOT["base_url"],
            api_key="no-auth", judge_model="gpt-4o-mini", judge_provider="openai",
            judge_api_key="sk-test-dummy", judge_base_url="http://127.0.0.1:9/v1",
            max_turns=1, show_progress=False, verbose=False,
        ))

    asyncio.run(run_with_client_cleanup(one_repetition, []))
    clients = _collect_async_clients(built[0])
    assert clients, "no async clients found on the decision auditor"
    assert all(getattr(c, "_client", c).is_closed for c in clients)


@pytest.mark.django_db
def test_the_repetition_runner_resolves_the_decision_key_before_the_loop(monkeypatch):
    """A snapshot with a connection_id resolves its key from the database, and
    AuditExperiment builds each repetition's auditor inside the running event
    loop, where Django refuses that lookup (SynchronousOnlyOperation). The key
    is resolved before the loop, as auditor_kwargs does for every role.
    """
    import simpleaudit.experiment as experiment_module

    from infra import engine

    assert SNAPSHOT["connection_id"], "the lookup only reaches the database with a connection"
    # run_scenario_repeated swaps the module's auditor class; put it back after.
    monkeypatch.setattr(experiment_module, "ModelAuditor", experiment_module.ModelAuditor)
    monkeypatch.setenv("STUDIO_TEST_KEY", "sk-test-dummy")
    built = []

    class FakeExperiment:
        def __init__(self, models, **_kwargs):
            self.entry = {k: v for k, v in models[0].items() if k != "label"}

        async def run_scenario_reps(self, **_kwargs):
            # What AuditExperiment does for every repetition, on the loop.
            built.append(experiment_module.ModelAuditor(**self.entry))
            return []

    other = {"model_id": "m", "provider": "openai", "base_url": "http://127.0.0.1:9/v1",
             "secret_reference": "STUDIO_TEST_KEY"}
    monkeypatch.setattr(experiment_module, "AuditExperiment", FakeExperiment)
    engine.run_scenario_repeated(
        name=SCENARIO["name"], description=SCENARIO["description"], expected_behavior=None,
        test_prompt=None, target=SNAPSHOT, auditor=other, judge=other, n_repetitions=1,
    )
    assert type(built[0].target).__name__ == "DecisionTarget"



@pytest.mark.django_db
def test_a_decision_auditor_never_flags_the_shared_class_while_it_is_built(monkeypatch):
    """Scenarios run in worker threads. While a decision auditor is being
    built, a chat auditor built at the same time on the shared ModelAuditor
    (or the trace-context subclass) must still get its chat client, so the
    skip has to live on the decision auditor's own class."""
    from simpleaudit.model_auditor import ModelAuditor

    from infra.trace_target import TraceContextModelAuditor, decision_auditor_class

    seen = []

    def client_factory(self, **_kwargs):
        # Runs inside ModelAuditor.__init__, i.e. while the auditor is built.
        seen.append((ModelAuditor.__dict__.get("_skip_target_client", False),
                     TraceContextModelAuditor.__dict__.get("_skip_target_client", False)))
        return object()

    monkeypatch.setattr(ModelAuditor, "_create_anyllm_client", client_factory)
    target = decision_target_for(SNAPSHOT)
    kwargs = {"model": SNAPSHOT["model_id"], "provider": "ollama", "base_url": SNAPSHOT["base_url"],
              "api_key": "no-auth", "judge_model": "j", "judge_provider": "openai", "max_turns": 1,
              "show_progress": False, "verbose": False}
    for auditor_cls in (without_target_client(ModelAuditor), decision_auditor_class(lambda: target)):
        assert auditor_cls._skip_target_client is True
        auditor_cls(**kwargs)
    assert seen and all(flags == (False, False) for flags in seen)


#: A decision snapshot whose default parameters carry ``extra_body``, which
#: ``_split_endpoint_params`` routes onto the per-request channel and
#: ``auditor_kwargs`` pops out of it as a separate kwarg.
SNAPSHOT_WITH_EXTRA_BODY = {**SNAPSHOT, "default_parameters": {"extra_body": {"session_id": "s-1"}}}


@pytest.mark.django_db
def test_a_decision_auditor_keeps_its_target_when_extra_body_is_injected(monkeypatch):
    """Both install paths call ``set_target``; on a decision run only the
    decision target may be installed, and the extra_body injection that follows
    must not bring the trace-context adapter back. It lands in target_params,
    which a DecisionTarget ignores."""
    from infra import engine

    monkeypatch.setenv("STUDIO_TEST_KEY", "sk-test-dummy")
    other = {"model_id": "m", "provider": "openai", "base_url": "http://127.0.0.1:9/v1",
             "secret_reference": "STUDIO_TEST_KEY"}
    instance, _language = engine.build_model_auditor(
        target=SNAPSHOT_WITH_EXTRA_BODY, auditor=other, judge=other,
    )
    assert type(instance.target).__name__ == "DecisionTarget"
    assert instance.target_params["extra_body"] == {"session_id": "s-1"}
    assert instance.max_turns == 1


@pytest.mark.django_db
def test_the_repetition_runner_builds_a_decision_auditor_with_extra_body(monkeypatch):
    """The model entry carries ``extra_body`` whenever the target's params do,
    and ModelAuditor takes no such kwarg: the per-repetition decision auditor
    pops it as TraceContextModelAuditor does, instead of failing to build."""
    import simpleaudit.experiment as experiment_module

    from infra import engine

    monkeypatch.setattr(experiment_module, "ModelAuditor", experiment_module.ModelAuditor)
    monkeypatch.setenv("STUDIO_TEST_KEY", "sk-test-dummy")
    built = []

    class FakeExperiment:
        def __init__(self, models, **_kwargs):
            self.entry = {k: v for k, v in models[0].items() if k != "label"}

        async def run_scenario_reps(self, **_kwargs):
            built.append(experiment_module.ModelAuditor(**self.entry))
            return []

    other = {"model_id": "m", "provider": "openai", "base_url": "http://127.0.0.1:9/v1",
             "secret_reference": "STUDIO_TEST_KEY"}
    monkeypatch.setattr(experiment_module, "AuditExperiment", FakeExperiment)
    engine.run_scenario_repeated(
        name=SCENARIO["name"], description=SCENARIO["description"], expected_behavior=None,
        test_prompt=None, target=SNAPSHOT_WITH_EXTRA_BODY, auditor=other, judge=other, n_repetitions=1,
    )
    assert type(built[0].target).__name__ == "DecisionTarget"
    assert built[0].target_params["extra_body"] == {"session_id": "s-1"}
