"""Using a decision-capable registered model as an audit target.

A decision model is not a chat model: it reads a state and answers a fixed
set of options with a choice, a probability per option and a calibrated
confidence — no prose, so no value to hallucinate. SimpleAudit 0.4.0 wraps
them in ``DecisionTarget``, with ``.ollama()`` for a local System One server
and ``.openrouter()`` for OpenRouter's Decisions API.

Which registered models are decision models is already settled on
/connections/: the discover probe persists the flag on
``RegisteredModel.capabilities`` and ``RegisteredModel.is_decision`` reads it.
This module is the step after that — turning such a model, or the frozen
endpoint snapshot a run keeps of it, into the ``DecisionTarget`` the engine
sends to, and shaping the answer that comes back for run detail.
"""

from __future__ import annotations

from typing import Any

#: The one provider whose decision endpoint is not ``{base_url}/v1/systemone``.
#: OpenRouter's Decisions API is a fixed, remote URL requiring a key, which is
#: why detection also treats it apart (model-id heuristic, not a probe).
OPENROUTER = "openrouter"


def is_decision_snapshot(snapshot: Any) -> bool:
    """Whether a frozen endpoint snapshot is of a decision-capable model.

    The snapshot copies ``RegisteredModel.capabilities`` verbatim
    (``audits.services._endpoint_snapshot``), so this is ``is_decision`` for a
    run that has already been frozen — the live model may have been re-probed
    or re-pointed since.
    """
    if not isinstance(snapshot, dict):
        return False
    return bool((snapshot.get("capabilities") or {}).get("decision"))


def build_decision_target(
    *, model_id: str, provider: str, base_url: str = "", api_key: str = "",
    factory: Any = None,
) -> Any:
    """A ``DecisionTarget`` for one model on one connection.

    The System One endpoint is a shared surface: every OpenAI-compatible
    server that has one exposes it at ``{base_url}/v1/systemone`` (Ollama
    >= 0.35, vLLM, …), which is why detection probes them all the same way
    (``services.probe_systemone_decision``). Target setup needs no
    per-provider endpoint logic either — only the auth handling. The
    ``.ollama()`` factory is what builds that URL, and it also applies the
    64 KiB request-body cap.

    OpenRouter is the exception: a fixed remote URL that needs a key, which
    Studio resolves from the connection's ``secret_reference`` (a raw key
    never enters a snapshot).

    ``factory`` is for tests: anything with ``.ollama()`` / ``.openrouter()``.
    The import stays inside the function so the registry keeps importing when
    the installed engine predates 0.4.0.
    """
    if factory is None:
        from simpleaudit import DecisionTarget as factory  # type: ignore[no-redef]

    if (provider or "").strip().lower() == OPENROUTER:
        if not api_key:
            raise ValueError(
                "A decision model on OpenRouter needs an API key: name the environment "
                "variable holding it in the connection's secret reference."
            )
        return factory.openrouter(model_id, api_key=api_key)

    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("A decision model needs a base URL, e.g. http://localhost:11434.")
    # The factory appends /v1/systemone, so a connection's own /v1 comes off
    # first — the same normalisation the capability probe does.
    extra = {"api_key": api_key} if api_key else {}
    return factory.ollama(model_id, base_url=base.removesuffix("/v1"), **extra)


def decision_target_for_model(model: Any, *, api_key: str = "", factory: Any = None) -> Any:
    """``build_decision_target`` for a live ``RegisteredModel``, gated on ``is_decision``."""
    if not getattr(model, "is_decision", False):
        raise ValueError(f"{model} is not marked as a decision model.")
    conn = model.connection
    return build_decision_target(
        model_id=model.model_id, provider=conn.provider,
        base_url=conn.base_url or "", api_key=api_key, factory=factory,
    )


def decision_target_for_snapshot(snapshot: Any, *, api_key: str = "", factory: Any = None) -> Any | None:
    """``build_decision_target`` for a frozen target snapshot, or ``None`` if it isn't one.

    Returning ``None`` rather than raising lets the engine call this on every
    run and only divert the ones that are decision runs.
    """
    if not is_decision_snapshot(snapshot):
        return None
    return build_decision_target(
        model_id=snapshot.get("model_id") or "", provider=snapshot.get("provider") or "",
        base_url=snapshot.get("base_url") or "", api_key=api_key, factory=factory,
    )


def decision_answer(message: Any) -> dict[str, Any] | None:
    """The structured answer SimpleAudit stores beside an assistant reply.

    ``ModelAuditor`` copies ``TargetResponse.decision`` onto the reply as
    ``decision``. Replies from a chat target have no such key, and this
    returns ``None`` for them. Options come back sorted by probability so the
    template can render them top-down without sorting in the view.
    """
    if not isinstance(message, dict):
        return None
    answer = message.get("decision")
    if not isinstance(answer, dict) or not answer.get("choice"):
        return None
    probs = answer.get("probabilities")
    options = []
    if isinstance(probs, dict):
        options = sorted(
            (
                {"option": k, "probability": v, "percent": round(float(v) * 100, 1)}
                for k, v in probs.items()
                if isinstance(v, (int, float))
            ),
            key=lambda o: -o["probability"],
        )
    confidence = answer.get("confidence")
    return {
        "choice": answer.get("choice"),
        "confidence": confidence,
        "confidence_percent": (
            round(float(confidence) * 100, 1) if isinstance(confidence, (int, float)) else None
        ),
        "options": options,
    }
