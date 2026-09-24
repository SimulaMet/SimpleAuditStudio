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
import sys
from typing import Any


class EngineError(RuntimeError):
    """Raised when the SimpleAudit engine cannot be loaded or a scenario fails."""


def _ensure_engine_available() -> None:
    """Ensure the SimpleAudit engine package is importable.

    The engine is a normal pip dependency (see ``requirements.txt``); there is no
    path-based fallback. If it is not installed (e.g. a web-only process or a
    test environment without the engine), raise a clean ``EngineError`` so the
    caller can record a durable failure instead of crashing on import.
    """
    try:
        import simpleaudit  # noqa: F401
        return
    except ModuleNotFoundError as exc:
        raise EngineError(
            "SimpleAudit engine is not installed. Install it from requirements.txt "
            "(pip install -r requirements.txt) before running audits."
        ) from exc


def _resolve_secret(secret_reference: str | None, api_key_direct: str | None = None) -> str | None:
    """Resolve credentials: direct key takes priority, then env var reference."""
    direct = (api_key_direct or "").strip()
    if direct:
        return direct
    ref = (secret_reference or "").strip()
    if not ref:
        return None
    return os.environ.get(ref)


def _validate_secrets(*snapshots: tuple[str, dict]) -> None:
    """Fail fast with a clear EngineError if any endpoint's secret is unresolved.

    ``any_llm`` raises an opaque ``MissingApiKeyError`` at client-construction time
    when no key is present. We surface that as a clean, actionable ``EngineError``
    naming the offending role + secret reference, so the worker records a readable
    failure instead of crashing mid-construction. A snapshot without a
    ``secret_reference`` (e.g. a local server needing no auth) is allowed.
    """
    for role, snap in snapshots:
        direct = (snap.get("api_key_direct") or "").strip()
        ref = (snap.get("secret_reference") or "").strip()
        if direct:
            continue  # direct key present, no need to validate env var
        if not ref:
            continue
        if _resolve_secret(ref) is None:
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


def _auditor_kwargs_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Map a frozen endpoint snapshot onto ModelAuditor constructor kwargs.

    Only the fields relevant to a given role are used; the caller picks which
    snapshot feeds target vs auditor vs judge.

    Returns a dict with keys: model, provider, base_url, api_key, kwargs
    (client constructor), gen_params (per-request generation params).
    """
    raw_params = dict(snapshot.get("default_parameters") or {})
    gen_params, client_kwargs = _split_endpoint_params(raw_params)
    base_url = snapshot.get("base_url") or None
    api_key = _resolve_secret(snapshot.get("secret_reference"), snapshot.get("api_key_direct"))
    # any_llm's OpenAI-compatible client requires *some* API key even when the
    # endpoint needs no auth (self-hosted / local servers). A snapshot without a
    # secret_reference means "no auth", so supply a non-secret placeholder — the
    # same convention the engine's own preflight probe uses (api_key="probe").
    if not api_key:
        api_key = "no-auth"
    return {
        "model": snapshot.get("model_id"),
        "provider": _normalize_provider(snapshot.get("provider"), base_url),
        "base_url": base_url,
        "api_key": api_key,
        "kwargs": client_kwargs,
        "gen_params": gen_params,
    }


def build_model_auditor(*, target: dict, auditor: dict, judge: dict, generation: dict | None = None):
    """Construct a ModelAuditor from three frozen endpoint snapshots.

    Returns the configured ``ModelAuditor`` instance. Raises ``EngineError`` if
    the engine cannot be imported.
    """
    _ensure_engine_available()
    try:
        from simpleaudit.model_auditor import ModelAuditor
    except Exception as exc:  # noqa: BLE001 - surface any import failure uniformly
        raise EngineError(f"Failed to import SimpleAudit ModelAuditor: {exc}") from exc

    gen = dict(generation or {})
    target_cfg = _auditor_kwargs_from_snapshot(target)
    auditor_cfg = _auditor_kwargs_from_snapshot(auditor)
    judge_cfg = _auditor_kwargs_from_snapshot(judge)

    # Fail fast with a clear, actionable error if a required secret is unset,
    # rather than letting any_llm raise an opaque MissingApiKeyError below.
    _validate_secrets(
        ("target", target), ("auditor", auditor), ("judge", judge)
    )

    # Generation parameters come from the frozen audit profile snapshot.
    max_turns = int(gen.get("max_turns") or 5)
    language = gen.get("language") or "English"
    max_retries = int(gen.get("max_retries") or 2)
    retry_backoff = float(gen.get("retry_backoff") or 0.5)
    system_prompt = gen.get("system_prompt") or None
    probe_prompt = gen.get("probe_prompt") or None
    judge_prompt = gen.get("judge_prompt") or None

    # SimpleAudit 0.1.13+ supports per-request generation params via
    # params / target_params / judge_params / auditor_params. These are
    # merged and passed directly into the LLM API call kwargs (temperature,
    # top_p, max_tokens, etc.). Constructor kwargs (timeout, headers, etc.)
    # still go through target_kwargs / auditor_kwargs / judge_kwargs.
    #
    # The generation config override JSON uses this structure:
    #   {"params": {...}, "target_params": {...}, "judge_params": {...},
    #    "auditor_params": {...}, "target_kwargs": {...}, ...}
    #
    # Keys under *_params → per-request generation params (any provider key)
    # Keys under *_kwargs → client constructor kwargs (timeout, headers, etc.)

    def _role_kwargs(cfg: dict[str, Any], gen_override: dict | None = None) -> dict[str, Any] | None:
        """Merge endpoint-level kwargs with generation-config constructor overrides."""
        raw = dict(cfg.get("kwargs") or {})
        if gen_override:
            raw.update(gen_override)
        return raw or None

    # Per-request generation params: merge endpoint-level gen_params with
    # the audit profile's generation config overrides.
    def _merge_gen_params(endpoint_gen: dict | None, profile_gen: dict | None) -> dict | None:
        merged = {}
        if endpoint_gen:
            merged.update(endpoint_gen)
        if profile_gen:
            merged.update(profile_gen)
        return merged or None

    gen_params = _merge_gen_params(target_cfg.get("gen_params"), gen.get("params"))
    gen_target_params = _merge_gen_params(target_cfg.get("gen_params"), gen.get("target_params"))
    gen_judge_params = _merge_gen_params(judge_cfg.get("gen_params"), gen.get("judge_params"))
    gen_auditor_params = _merge_gen_params(auditor_cfg.get("gen_params"), gen.get("auditor_params"))

    # Constructor kwargs overrides
    target_ctor_kwargs = gen.get("target_kwargs") or None
    auditor_ctor_kwargs = gen.get("auditor_kwargs") or None
    judge_ctor_kwargs = gen.get("judge_kwargs") or None

    try:
        instance = ModelAuditor(
            model=target_cfg["model"],
            provider=target_cfg["provider"],
            base_url=target_cfg["base_url"],
            api_key=target_cfg["api_key"],
            target_kwargs=_role_kwargs(target_cfg, target_ctor_kwargs),
            auditor_model=auditor_cfg["model"],
            auditor_provider=auditor_cfg["provider"],
            auditor_base_url=auditor_cfg["base_url"],
            auditor_api_key=auditor_cfg["api_key"],
            auditor_kwargs=_role_kwargs(auditor_cfg, auditor_ctor_kwargs),
            judge_model=judge_cfg["model"],
            judge_provider=judge_cfg["provider"],
            judge_base_url=judge_cfg["base_url"],
            judge_api_key=judge_cfg["api_key"],
            judge_kwargs=_role_kwargs(judge_cfg, judge_ctor_kwargs),
            params=gen_params,
            target_params=gen_target_params,
            judge_params=gen_judge_params,
            auditor_params=gen_auditor_params,
            max_turns=max_turns,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            system_prompt=system_prompt,
            probe_prompt=probe_prompt,
            judge_prompt=judge_prompt,
            show_progress=False,
            verbose=False,
        )
    except EngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - any construction failure (bad base_url, provider, etc.)
        raise EngineError(f"Failed to construct ModelAuditor: {type(exc).__name__}: {exc}") from exc
    return instance, language


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
    try:
        result = asyncio.run(
            auditor_instance.run_scenario(
                name=name,
                description=description,
                expected_behavior=expected_behavior,
                test_prompt=test_prompt,
                language=language,
                on_turn=on_turn,
            )
        )
    except EngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - unexpected engine crash
        raise EngineError(f"Scenario execution crashed: {type(exc).__name__}: {exc}") from exc

    payload = result.to_dict()
    payload["_language"] = language
    return payload


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
    gen = dict(generation or {})
    language = gen.get("language") or "English"

    # Build the scenario dict in the format the engine expects
    scenario: dict[str, Any] = {
        "name": name,
        "description": description,
    }
    if expected_behavior:
        scenario["expected_behavior"] = expected_behavior
    if test_prompt:
        scenario["test_prompt"] = test_prompt

    # Build a single-model AuditExperiment configured from the frozen snapshots.
    # This reuses the engine's _merge_common + ModelAuditor construction path,
    # including its retry logic and error handling.
    _ensure_engine_available()
    try:
        from simpleaudit.experiment import AuditExperiment
    except ImportError:
        # Fallback: older engine versions without AuditExperiment.run_scenario_reps
        return _run_scenario_repeated_fallback(
            name=name, description=description,
            expected_behavior=expected_behavior, test_prompt=test_prompt,
            target=target, auditor=auditor, judge=judge,
            generation=generation, n_repetitions=n_repetitions,
            on_rep_done=on_rep_done,
        )

    # Construct the model config from the frozen target snapshot
    target_cfg = _auditor_kwargs_from_snapshot(target)
    auditor_cfg = _auditor_kwargs_from_snapshot(auditor)
    judge_cfg = _auditor_kwargs_from_snapshot(judge)

    _validate_secrets(("target", target), ("auditor", auditor), ("judge", judge))

    max_turns = int(gen.get("max_turns") or 5)
    max_retries = int(gen.get("max_retries") or 2)
    retry_backoff = float(gen.get("retry_backoff") or 0.5)
    system_prompt = gen.get("system_prompt") or None
    probe_prompt = gen.get("probe_prompt") or None
    judge_prompt = gen.get("judge_prompt") or None

    # Merge endpoint-level gen_params with profile-level overrides
    def _merge_gen(endpoint_gen: dict | None, profile_gen: dict | None) -> dict | None:
        merged = {}
        if endpoint_gen:
            merged.update(endpoint_gen)
        if profile_gen:
            merged.update(profile_gen)
        return merged or None

    gen_params = _merge_gen(target_cfg.get("gen_params"), gen.get("params"))
    gen_target_params = _merge_gen(target_cfg.get("gen_params"), gen.get("target_params"))
    gen_judge_params = _merge_gen(judge_cfg.get("gen_params"), gen.get("judge_params"))
    gen_auditor_params = _merge_gen(auditor_cfg.get("gen_params"), gen.get("auditor_params"))

    def _role_kwargs(cfg: dict[str, Any], gen_override: dict | None = None) -> dict[str, Any] | None:
        raw = dict(cfg.get("kwargs") or {})
        if gen_override:
            raw.update(gen_override)
        return raw or None

    target_ctor_kwargs = gen.get("target_kwargs") or None
    auditor_ctor_kwargs = gen.get("auditor_kwargs") or None
    judge_ctor_kwargs = gen.get("judge_kwargs") or None

    # Single-model experiment: the model entry carries the target config,
    # while judge/auditor are set at the experiment level.
    # All keys here flow through _merge_common → ModelAuditor(**merged).
    model_entry: dict[str, Any] = {
        "model": target_cfg["model"],
        "provider": target_cfg["provider"],
        "base_url": target_cfg["base_url"],
        "api_key": target_cfg["api_key"],
        "label": f"{target_cfg['model']} (platform)",
        # Constructor kwargs for the any_llm client (timeout, headers, etc.)
        "kwargs": _role_kwargs(target_cfg, target_ctor_kwargs),
        # Per-request generation params (temperature, top_p, max_tokens, etc.)
        "params": gen_params,
        "target_params": gen_target_params,
        "judge_params": gen_judge_params,
        "auditor_params": gen_auditor_params,
        # Retry config from the frozen generation profile
        "max_retries": max_retries,
        "retry_backoff": retry_backoff,
        # System prompt (if configured)
        "system_prompt": system_prompt,
    }
    # Remove None values — ModelAuditor treats None differently from absent
    # for some fields (e.g., params=None means "no override" which is fine,
    # but we want to be explicit).
    model_entry = {k: v for k, v in model_entry.items() if v is not None}

    # Track rep completions via callback.
    # NOTE: The engine's on_rep_done receives an AuditResults collection
    # (one per rep). For single-scenario execution it always contains exactly
    # one AuditResult. We extract that single result for the platform's
    # per-rep storage format.
    #
    # IMPORTANT: The outer on_rep_done (from the worker) may do Django ORM
    # calls, which CANNOT run inside asyncio.run()'s event loop. So we collect
    # results here and emit events AFTER asyncio.run() returns.
    reps: list[dict[str, Any]] = []

    def _on_rep_done(label: str, rep_index: int, total: int, result) -> None:
        if result is None:
            return
        # result is AuditResults (collection); extract the single scenario result
        single = result[0] if hasattr(result, '__getitem__') and len(result) > 0 else None
        if single is None:
            return
        payload = single.to_dict()
        payload["_rep_index"] = rep_index
        reps.append(payload)
        # Do NOT call on_rep_done here — it may hit the DB from an async context.
        # Events are emitted after asyncio.run() completes (see below).

    # Constructor kwargs for judge/auditor clients go in the model entry dict
    # because AuditExperiment.__init__ doesn't accept them directly — they flow
    # through _merge_common → ModelAuditor(**merged).
    judge_kw = _role_kwargs(auditor_cfg, judge_ctor_kwargs)
    if judge_kw:
        model_entry["judge_kwargs"] = judge_kw
    auditor_kw = _role_kwargs(auditor_cfg, auditor_ctor_kwargs)
    if auditor_kw:
        model_entry["auditor_kwargs"] = auditor_kw

    try:
        experiment = AuditExperiment(
            models=[model_entry],
            judge_model=auditor_cfg["model"],
            judge_provider=auditor_cfg["provider"],
            judge_base_url=auditor_cfg["base_url"],
            judge_api_key=auditor_cfg["api_key"],
            auditor_model=auditor_cfg["model"],
            auditor_provider=auditor_cfg["provider"],
            auditor_base_url=auditor_cfg["base_url"],
            auditor_api_key=auditor_cfg["api_key"],
            probe_prompt=probe_prompt,
            judge_prompt=judge_prompt,
            json_format=True,
            verbose=False,
            show_progress=False,
            n_repetitions=n_repetitions,
            on_rep_done=_on_rep_done,
            cancel_event=cancel_event,
            max_retries_per_rep=max_retries,
        )
    except Exception as exc:
        raise EngineError(f"Failed to construct AuditExperiment: {type(exc).__name__}: {exc}") from exc

    try:
        results = asyncio.run(
            experiment.run_scenario_reps(
                model_index=0,
                scenario=scenario,
                max_turns=max_turns,
                language=language,
                on_turn=on_turn,
            )
        )
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError(f"Scenario execution crashed: {type(exc).__name__}: {exc}") from exc

    # If the callback didn't fire (e.g., all reps were cached/skipped),
    # fall back to the returned results list.
    if not reps and results:
        for i, r in enumerate(results):
            payload = r.to_dict()
            payload["_rep_index"] = i
            reps.append(payload)

    # Emit progress events NOW (safe: we're back in sync context).
    if on_rep_done:
        for rep in reps:
            on_rep_done(rep.get("_rep_index", 0), rep)

    # --- Aggregate stability stats ---
    severities = [r.get("severity", "") for r in reps]
    sev_counts: dict[str, int] = {}
    for s in severities:
        sev_counts[s] = sev_counts.get(s, 0) + 1

    _SEV_RANK = {"ERROR": 6, "critical": 5, "high": 4, "medium": 3, "low": 2, "pass": 1}
    modal_severity = max(sev_counts.keys(), key=lambda s: (sev_counts[s], _SEV_RANK.get(s, 0))) if sev_counts else "ERROR"
    agreement_rate = sev_counts[modal_severity] / len(reps) if reps else 0.0

    return {
        "reps": reps,
        "aggregated_severity": modal_severity,
        "agreement_rate": round(agreement_rate, 4),
        "severity_distribution": sev_counts,
        "n_repetitions": len(reps),
        "_language": language,
    }



def _run_scenario_repeated_fallback(
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
) -> dict[str, Any]:
    """Fallback for engine versions without AuditExperiment.run_scenario_reps."""
    gen = dict(generation or {})
    reps: list[dict[str, Any]] = []
    language = None

    for i in range(n_repetitions):
        auditor_instance, language = build_model_auditor(
            target=target, auditor=auditor, judge=judge, generation=gen
        )
        try:
            result = asyncio.run(
                auditor_instance.run_scenario(
                    name=name,
                    description=description,
                    expected_behavior=expected_behavior,
                    test_prompt=test_prompt,
                    language=language,
                )
            )
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(f"Scenario execution crashed (rep {i+1}): {type(exc).__name__}: {exc}") from exc

        rep_payload = result.to_dict()
        rep_payload["_language"] = language
        rep_payload["_rep_index"] = i
        reps.append(rep_payload)

        if on_rep_done:
            on_rep_done(i, rep_payload)

    severities = [r.get("severity", "") for r in reps]
    sev_counts: dict[str, int] = {}
    for s in severities:
        sev_counts[s] = sev_counts.get(s, 0) + 1

    _SEV_RANK = {"ERROR": 6, "critical": 5, "high": 4, "medium": 3, "low": 2, "pass": 1}
    modal_severity = max(sev_counts.keys(), key=lambda s: (sev_counts[s], _SEV_RANK.get(s, 0))) if sev_counts else "ERROR"
    agreement_rate = sev_counts[modal_severity] / len(reps) if reps else 0.0

    return {
        "reps": reps,
        "aggregated_severity": modal_severity,
        "agreement_rate": round(agreement_rate, 4),
        "severity_distribution": sev_counts,
        "n_repetitions": len(reps),
        "_language": language,
    }
