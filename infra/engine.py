"""SimpleAudit engine integration.

This is the ONLY place in the platform that imports the SimpleAudit engine. It is
imported lazily so the web process, tests, and the worker's import-time do not
require the engine to be installed or reachable — only an actual scenario
execution needs it.

The platform WRAPS the existing engine; it does not reimplement Target ->
Auditor -> Judge semantics. A single scenario is executed by building a
``ModelAuditor`` from the FROZEN endpoint snapshots stored on the AuditRun (never
from live registry rows) and calling ``run_scenario`` once.

Secrets are resolved at execution time from the environment using the
``secret_reference`` identifier stored on each endpoint snapshot. Raw credentials
are never persisted in the database.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any


class EngineError(RuntimeError):
    """Raised when the SimpleAudit engine cannot be loaded or a scenario fails."""


def _ensure_engine_available() -> None:
    """Ensure the SimpleAudit engine package is importable.

    The engine is a normal pip dependency (declared in ``pyproject.toml``);
    there is no path-based fallback. If it is not installed (e.g. a web-only
    process or a test environment without the engine), raise a clean
    ``EngineError`` so the caller can record a durable failure instead of
    crashing on import.
    """
    try:
        import simpleaudit  # noqa: F401
        return
    except ModuleNotFoundError as exc:
        raise EngineError(
            "SimpleAudit engine is not installed. Install the project "
            "dependencies (uv sync) before running audits."
        ) from exc


def _resolve_secret(secret_reference: str | None) -> str | None:
    """The value of the environment variable a connection names, or None."""
    ref = (secret_reference or "").strip()
    if not ref:
        return None
    return os.environ.get(ref) or None


def snapshot_api_key(snapshot: dict) -> str | None:
    """API key for a frozen endpoint snapshot, resolved at execution time.

    Snapshots never store raw keys. The key comes from the snapshot's connection
    (its stored key, else the env var it names); when the connection is gone,
    from the snapshot's own ``secret_reference``.
    """
    from model_registry.models import ModelConnection
    from model_registry.services import connection_api_key

    conn_id = snapshot.get("connection_id")
    conn = ModelConnection.objects.filter(pk=conn_id).first() if conn_id else None
    if conn is not None:
        return connection_api_key(conn) or None
    return _resolve_secret(snapshot.get("secret_reference"))


def _validate_secrets(*snapshots: tuple[str, dict]) -> None:
    """Fail fast with a clear EngineError if any endpoint's secret is unresolved.

    ``any_llm`` raises an opaque ``MissingApiKeyError`` at client-construction time
    when no key is present. We surface that as a clean, actionable ``EngineError``
    naming the offending role + secret reference, so the worker records a readable
    failure instead of crashing mid-construction. A snapshot without a
    ``secret_reference`` (e.g. a local server needing no auth) is allowed.
    """
    for role, snap in snapshots:
        ref = (snap.get("secret_reference") or "").strip()
        if not ref or snapshot_api_key(snap):
            continue   # no auth needed, or a key was found
        raise EngineError(
            f"Secret for {role} endpoint is not set: environment variable "
            f"'{ref}' is missing or empty. Set it in the worker environment."
        )


# Providers that any_llm does not know about are treated as OpenAI-compatible
# gateways/self-hosted servers (the common case for local models and proxies).
_KNOWN_ANYLLM_PROVIDERS = {
    "anthropic", "bedrock", "azure", "azureanthropic", "azureopenai", "cerebras",
    "cohere", "deepseek", "fireworks", "gemini", "github", "groq", "huggingface",
    "llama", "lmstudio", "llamafile", "llamacpp", "meta", "mistral", "moonshot",
    "ollama", "openai", "openrouter", "perplexity", "sambanova", "together",
    "vllm", "xai", "dashscope", "deepinfra", "minimax", "zai",
}


def _normalize_provider(provider: str | None, base_url: str | None) -> str:
    """Return an any_llm provider key.

    A recognised provider is passed through. An unrecognised label (e.g. a
    registry display name such as ``simulachat``) combined with a base URL is
    treated as an OpenAI-compatible endpoint — how self-hosted and gateway
    deployments expose their API.
    """
    p = (provider or "").strip().lower()
    if p in _KNOWN_ANYLLM_PROVIDERS:
        return p
    if base_url:
        return "openai"
    return p or "openai"


# Generation params that belong in per-request params (NOT client constructor)
_GENERATION_PARAM_KEYS = {
    "temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens",
    "frequency_penalty", "presence_penalty", "stop", "seed", "logprobs",
    "n", "response_format", "tools", "tool_choice", "functions",
}


def _split_endpoint_params(params: dict[str, Any]) -> tuple[dict | None, dict | None]:
    """Split endpoint default_parameters into (generation_params, client_kwargs).

    Generation params (temperature, top_p, etc.) go per-request via ModelAuditor's
    ``params``/``target_params``. Client constructor params (timeout, headers, etc.)
    go via ``kwargs``/``target_kwargs``.
    """
    gen_params: dict[str, Any] = {}
    client_kwargs: dict[str, Any] = {}
    for k, v in params.items():
        if k in _GENERATION_PARAM_KEYS:
            gen_params[k] = v
        else:
            client_kwargs[k] = v
    return (gen_params or None), (client_kwargs or None)


# A slow or stuck endpoint must fail and be retried, not hang a run: the
# OpenAI client's own default is 600 s per request with 2 retries, and
# SimpleAudit retries on top. An endpoint's default_parameters can set
# "timeout" / "max_retries" to override these.
CLIENT_TIMEOUT_S = 180
CLIENT_MAX_RETRIES = 1
_CLIENT_DEFAULTS: dict[str, dict] = {}


def _client_defaults(provider: str) -> dict:
    """``timeout`` / ``max_retries`` client kwargs, when the provider's client
    takes them (probed once by building one; construction opens no connection).
    Providers that don't, or can't be built here, get none."""
    cached = _CLIENT_DEFAULTS.get(provider)
    if cached is None:
        defaults = {"timeout": CLIENT_TIMEOUT_S, "max_retries": CLIENT_MAX_RETRIES}
        try:
            from any_llm import AnyLLM

            AnyLLM.create(provider, api_key="probe", **defaults)
            cached = defaults
        except Exception:  # noqa: BLE001 - unsupported kwargs or provider extras not installed
            cached = {}
        _CLIENT_DEFAULTS[provider] = cached
    return dict(cached)


def _auditor_kwargs_from_snapshot(snapshot: dict[str, Any], resolve_key=snapshot_api_key) -> dict[str, Any]:
    """Map a frozen endpoint snapshot onto ModelAuditor constructor kwargs.

    Only the fields relevant to a given role are used; the caller picks which
    snapshot feeds target vs auditor vs judge.

    Returns a dict with keys: model, provider, base_url, api_key, kwargs
    (client constructor), gen_params (per-request generation params).
    """
    raw_params = dict(snapshot.get("default_parameters") or {})
    gen_params, client_kwargs = _split_endpoint_params(raw_params)
    base_url = snapshot.get("base_url") or None
    api_key = resolve_key(snapshot)
    # any_llm's OpenAI-compatible client requires *some* API key even when the
    # endpoint needs no auth (self-hosted / local servers). A snapshot without a
    # secret_reference means "no auth", so supply a non-secret placeholder — the
    # same convention the engine's own preflight probe uses (api_key="probe").
    if not api_key:
        api_key = "no-auth"
    provider = _normalize_provider(snapshot.get("provider"), base_url)
    return {
        "model": snapshot.get("model_id"),
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "kwargs": {**_client_defaults(provider), **(client_kwargs or {})} or None,
        "gen_params": gen_params,
    }


def _merge(*dicts: dict | None) -> dict | None:
    merged: dict[str, Any] = {}
    for d in dicts:
        if d:
            merged.update(d)
    return merged or None


def auditor_kwargs(*, target: dict, auditor: dict, judge: dict, generation: dict | None = None,
                   resolve_key=None) -> tuple[dict, str]:
    """``ModelAuditor`` constructor kwargs from the three frozen snapshots, plus the language.

    The single place that maps a run onto the engine, used by both the
    single-repetition path and the multi-repetition ``AuditExperiment`` path.

    Generation config keys: ``params`` (all roles), ``target_params`` /
    ``judge_params`` / ``auditor_params`` are per-request generation params
    (temperature, top_p, max_tokens, ...), merged over each endpoint's own
    defaults. ``target_kwargs`` / ``auditor_kwargs`` / ``judge_kwargs`` are
    client constructor kwargs (timeout, headers, ...).

    ``resolve_key(snapshot)`` supplies each endpoint's API key. By default the
    real key is resolved (and a missing one is an error); script generation
    (infra.codegen) passes its own, which yields environment lookups instead.
    """
    from judges.services import library_judge

    gen = dict(generation or {})
    spec = (judge.get("judge") or {}).get("spec") or {}
    if resolve_key is None:
        # Fail fast with a clear, actionable error if a required secret is unset,
        # rather than letting any_llm raise an opaque MissingApiKeyError later.
        _validate_secrets(("target", target), ("auditor", auditor), ("judge", judge))
        resolve_key = snapshot_api_key
    target_cfg = _auditor_kwargs_from_snapshot(target, resolve_key)
    auditor_cfg = _auditor_kwargs_from_snapshot(auditor, resolve_key)
    judge_cfg = _auditor_kwargs_from_snapshot(judge, resolve_key)

    kwargs = {
        "model": target_cfg["model"],
        "provider": target_cfg["provider"],
        "base_url": target_cfg["base_url"],
        "api_key": target_cfg["api_key"],
        "target_kwargs": _merge(target_cfg["kwargs"], gen.get("target_kwargs")),
        "auditor_model": auditor_cfg["model"],
        "auditor_provider": auditor_cfg["provider"],
        "auditor_base_url": auditor_cfg["base_url"],
        "auditor_api_key": auditor_cfg["api_key"],
        "auditor_kwargs": _merge(auditor_cfg["kwargs"], gen.get("auditor_kwargs")),
        "judge_model": judge_cfg["model"],
        "judge_provider": judge_cfg["provider"],
        "judge_base_url": judge_cfg["base_url"],
        "judge_api_key": judge_cfg["api_key"],
        "judge_kwargs": _merge(judge_cfg["kwargs"], gen.get("judge_kwargs")),
        "params": _merge(target_cfg["gen_params"], gen.get("params")),
        "target_params": _merge(target_cfg["gen_params"], gen.get("target_params")),
        "judge_params": _merge(judge_cfg["gen_params"], gen.get("judge_params")),
        "auditor_params": _merge(auditor_cfg["gen_params"], gen.get("auditor_params")),
        "max_turns": int(gen.get("max_turns") or 5),
        "max_retries": int(gen.get("max_retries") or 2),
        "retry_backoff": float(gen.get("retry_backoff") or 0.5),
        "system_prompt": gen.get("system_prompt") or None,
        # The frozen judge, built from its spec: an unedited SimpleAudit
        # judge by name, else a composed config (criteria + output format).
        "judge": library_judge(spec) if spec else None,
        "probe_prompt": spec.get("probe_prompt") or None,
        "json_format": True,
        "show_progress": False,
        "verbose": False,
    }
    return kwargs, gen.get("language") or "English"


def build_model_auditor(*, target: dict, auditor: dict, judge: dict, generation: dict | None = None):
    """Construct a ModelAuditor from three frozen endpoint snapshots.

    Returns ``(instance, language)``. Raises ``EngineError`` if the engine
    cannot be imported or the auditor cannot be built.
    """
    _ensure_engine_available()
    try:
        from simpleaudit.model_auditor import ModelAuditor
    except Exception as exc:
        raise EngineError(f"Failed to import SimpleAudit ModelAuditor: {exc}") from exc

    kwargs, language = auditor_kwargs(target=target, auditor=auditor, judge=judge, generation=generation)
    try:
        instance = ModelAuditor(**kwargs)
    except Exception as exc:
        raise EngineError(f"Failed to construct ModelAuditor: {type(exc).__name__}: {exc}") from exc
    return instance, language


def scenario_dict(
    *,
    name: str,
    description: str,
    expected_behavior: list[str] | None = None,
    test_prompt: str | None = None,
    severity_ceiling: str = "",
    documents: list | None = None,
    file_uri=None,
    category: str = "",
    metadata: dict | None = None,
) -> dict[str, Any]:
    """A scenario in SimpleAudit's own format (what ``ModelAuditor.run`` and
    ``AuditExperiment`` take), with empty fields left out. The designed
    severity goes in ``severity``; judge notes ride in ``metadata``."""
    scenario: dict[str, Any] = {"name": name, "description": description}
    optional = {
        "expected_behavior": expected_behavior, "test_prompt": test_prompt, "severity": severity_ceiling,
        "documents": documents, "file_uri": file_uri, "category": category, "metadata": metadata,
    }
    scenario.update({k: v for k, v in optional.items() if v})
    return scenario


def run_scenario(
    *,
    name: str,
    description: str,
    expected_behavior: list[str] | None,
    test_prompt: str | None,
    target: dict,
    auditor: dict,
    judge: dict,
    generation: dict | None = None,
    on_turn: callable | None = None,
    severity_ceiling: str = "",
    documents: list | None = None,
    file_uri=None,
    category: str = "",
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Execute one scenario through the real engine and return a serializable result.

    Runs the async ``ModelAuditor.run_scenario`` to completion and returns
    ``AuditResult.to_dict()`` plus the language used. Raises ``EngineError`` on
    load failure; a mid-conversation/judging failure is captured by the engine
    itself as a severity of ``ERROR`` in the returned dict (not raised), matching
    the engine's own error-handling contract.

    If ``on_turn`` is provided, it is called at each phase boundary with
    ``(turn_index, max_turns, role)`` where role is "auditor", "target", or "judge".
    NOTE: on_turn is called from within the asyncio event loop — do NOT perform
    blocking I/O (e.g. Django ORM) inside it.
    """
    auditor_instance, language = build_model_auditor(
        target=target, auditor=auditor, judge=judge, generation=generation
    )
    scenario = scenario_dict(
        name=name, description=description, expected_behavior=expected_behavior, test_prompt=test_prompt,
        severity_ceiling=severity_ceiling, documents=documents, file_uri=file_uri, category=category,
        metadata=metadata,
    )
    try:
        # run_async maps the scenario dict onto run_scenario (file_uri,
        # documents, judge notes, the scenario facts a judge's post-processor
        # reads), the same way AuditExperiment does for repetitions.
        results = asyncio.run(auditor_instance.run_async([scenario], language=language, on_turn=on_turn))
    except Exception as exc:
        raise EngineError(f"Scenario execution crashed: {type(exc).__name__}: {exc}") from exc

    payload = results[0].to_dict()
    payload["_language"] = language
    return payload


_SEV_RANK = {"ERROR": 6, "critical": 5, "high": 4, "medium": 3, "low": 2, "pass": 1}


def run_scenario_repeated(
    *,
    name: str,
    description: str,
    expected_behavior: list[str] | None,
    test_prompt: str | None,
    target: dict,
    auditor: dict,
    judge: dict,
    generation: dict | None = None,
    n_repetitions: int = 1,
    on_rep_done: callable | None = None,
    on_turn: callable | None = None,
    on_rep_started: callable | None = None,
    cancel_event: asyncio.Event | None = None,
    severity_ceiling: str = "",
    documents: list | None = None,
    file_uri=None,
    category: str = "",
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Execute one scenario N times using AuditExperiment.run_scenario_reps().

    Delegates to the SimpleAudit engine's native multi-rep execution, which
    provides: fresh ModelAuditor per rep, auto-retry on ERROR, cancellation,
    and typed callbacks.

    The returned dict contains:
      - ``reps``: list of per-repetition result dicts (same shape as run_scenario)
      - ``aggregated_severity``: modal severity across reps
      - ``agreement_rate``: fraction of reps matching the modal severity
      - ``severity_distribution``: {severity: count}
      - ``n_repetitions``: number of reps actually executed
      - ``_language``: language used

    If ``on_rep_done`` is provided it is called after each rep with
    ``(rep_index, rep_result_dict)`` — useful for emitting progress events.
    If ``cancel_event`` is set, remaining reps are skipped.
    """
    _ensure_engine_available()
    try:
        from simpleaudit.experiment import AuditExperiment
    except Exception as exc:
        raise EngineError(f"Failed to import SimpleAudit AuditExperiment: {exc}") from exc

    kwargs, language = auditor_kwargs(target=target, auditor=auditor, judge=judge, generation=generation)
    max_turns = kwargs["max_turns"]
    scenario = scenario_dict(
        name=name, description=description, expected_behavior=expected_behavior, test_prompt=test_prompt,
        severity_ceiling=severity_ceiling, documents=documents, file_uri=file_uri, category=category,
        metadata=metadata,
    )

    # The model entry carries every ModelAuditor kwarg (target, auditor and
    # judge alike): AuditExperiment passes it through _merge_common to
    # ModelAuditor(**entry), and entry values win over experiment-level ones.
    model_entry = {k: v for k, v in kwargs.items() if v is not None}
    model_entry["label"] = f"{kwargs['model']} (platform)"

    # The engine's on_rep_done receives an AuditResults collection with one
    # result (single scenario). Collect here and emit AFTER asyncio.run()
    # returns: the outer callback does Django ORM calls, which cannot run
    # inside the event loop.
    reps: list[dict[str, Any]] = []

    def _on_rep_done(label: str, rep_index: int, total: int, result) -> None:
        if not result:
            return
        payload = result[0].to_dict()
        payload["_rep_index"] = rep_index
        reps.append(payload)

    # ``rep_is_done(label, i)`` is consulted right before rep ``i`` starts —
    # the only hook that fires at every rep boundary. Use it to signal rep
    # starts; never skip. on_rep_started must be async-safe (no ORM calls).
    def _rep_is_done(label: str, rep_index: int) -> bool:
        if on_rep_started:
            on_rep_started(rep_index)
        return False

    try:
        experiment = AuditExperiment(
            models=[model_entry],
            n_repetitions=n_repetitions,
            on_rep_done=_on_rep_done,
            rep_is_done=_rep_is_done,
            cancel_event=cancel_event,
            max_retries_per_rep=kwargs["max_retries"],
            json_format=True,
            verbose=False,
            show_progress=False,
        )
    except Exception as exc:
        raise EngineError(f"Failed to construct AuditExperiment: {type(exc).__name__}: {exc}") from exc

    try:
        results = asyncio.run(
            experiment.run_scenario_reps(
                model_index=0, scenario=scenario, max_turns=max_turns, language=language, on_turn=on_turn,
            )
        )
    except Exception as exc:
        raise EngineError(f"Scenario execution crashed: {type(exc).__name__}: {exc}") from exc

    # If the callback didn't fire (e.g. all reps cached), use the returned list.
    if not reps and results:
        for i, r in enumerate(results):
            payload = r.to_dict()
            payload["_rep_index"] = i
            reps.append(payload)

    if on_rep_done:
        for rep in reps:
            on_rep_done(rep.get("_rep_index", 0), rep)

    sev_counts: dict[str, int] = {}
    for rep in reps:
        sev = rep.get("severity", "")
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
    modal_severity = max(sev_counts, key=lambda s: (sev_counts[s], _SEV_RANK.get(s, 0))) if sev_counts else "ERROR"
    agreement_rate = sev_counts[modal_severity] / len(reps) if reps else 0.0

    return {
        "reps": reps,
        "aggregated_severity": modal_severity,
        "agreement_rate": round(agreement_rate, 4),
        "severity_distribution": sev_counts,
        "n_repetitions": len(reps),
        "_language": language,
    }
