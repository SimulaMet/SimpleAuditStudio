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
from contextlib import nullcontext
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
    "n", "response_format", "tools", "tool_choice", "functions", "reasoning_effort",
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
        if k in _GENERATION_PARAM_KEYS or k == "extra_body":
            # ``extra_body`` is a per-request parameter (session_id / tool_ids
            # for Open WebUI's saved-chat path), not a client constructor arg.
            # It rides the same per-request channel as the generation fields;
            # auditor_kwargs() later pops it out of the merged dicts so it
            # never leaks into AsyncOpenAI.__init__.
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


def augment_agent_generation(
    generation: dict | None, agent_snapshot: dict | None, *, via_wrapper: bool = False,
    target: dict | None = None,
) -> dict:
    """Add standard Open WebUI Agent request metadata to target parameters.

    Open WebUI's compatibility endpoint only exposes model-attached native
    tools/knowledge to requests that carry a session.  The frozen Agent
    snapshot is the execution source of truth, so forward its tool IDs through
    the endpoint's standard ``extra_body`` fields without inventing a protocol
    understood by the target model.
    """
    result = dict(generation or {})
    if not agent_snapshot:
        return result
    result["_agent_target"] = True
    base_model = agent_snapshot.get("base_model") or {}
    # Both traced and untraced runs execute the agent's base model directly with
    # tools injected as OpenAI function definitions. Traced runs additionally
    # route through the OWUI compat endpoint (which exports OTLP spans) rather
    # than the base connection, so the W3C traceparent propagated per turn is
    # adopted by OWUI's exporter and the spans land in Studio's listener.
    if base_model.get("connection_id") and base_model.get("model_id"):
        result["_agent_base_model"] = {
            "connection_id": base_model["connection_id"],
            "model_id": base_model.get("model_id"),
        }
    # The frozen Agent snapshot is the execution source of truth for the
    # target's persona. When a run does not carry an explicit system prompt,
    # use the agent's — this also makes bare-model targets (e.g. SimulaChat
    # directly) behave as the audited agent instead of a generic assistant.
    # The frozen persona names the agent's tools as OWUI internal function
    # names; when we execute the base model directly with OpenAI function
    # definitions, append a short execution note so the persona and the actual
    # tool names line up. Without it the model follows stale instructions like
    # "use acme_lookup_order" and never calls the real functions.
    if not result.get("system_prompt"):
        prompt = (agent_snapshot.get("system_prompt") or "").strip()
        if prompt:
            tool_names = [
                t.get("external_id") for t in agent_snapshot.get("tools") or []
                if isinstance(t, dict) and t.get("external_id")
            ]
            if (agent_snapshot.get("capabilities") or {}).get("knowledge_search"):
                tool_names.append("acme_search_knowledge")
            if tool_names:
                prompt += (
                    "\n\nExecution note: you are running with these exact tools — "
                    + ", ".join(f"`{name}`" for name in dict.fromkeys(tool_names))
                    + ". Call them by these names whenever the task needs them."
                )
            result["system_prompt"] = prompt
    target_params = dict(result.get("target_params") or {})
    # Open WebUI's Agent compatibility endpoint does not accept provider-only
    # reasoning controls; the Agent's base provider owns that configuration.
    target_params.pop("reasoning_effort", None)
    # Inject the agent's tools as OpenAI function-calling definitions so a
    # bare-model target (e.g. SimulaChat directly) can actually invoke them.
    # Without this the LLM has no idea which functions are available and the
    # deterministic tool.required check always fails.
    agent_tools = []
    for tool in agent_snapshot.get("tools") or []:
        if not (isinstance(tool, dict) and tool.get("external_id")):
            continue
        raw_schema = tool.get("input_schema")
        # The schema may be stored as a JSON string in the DB — parse it.
        if isinstance(raw_schema, str) and raw_schema.strip():
            import json as _json
            try:
                raw_schema = _json.loads(raw_schema)
            except (_json.JSONDecodeError, ValueError):
                raw_schema = None
        # Case 1: input_schema is a list of OpenAI function definitions
        # (one OWUI tool can expose multiple sub-functions).
        if isinstance(raw_schema, list):
            for entry in raw_schema:
                if isinstance(entry, dict) and "function" in entry:
                    agent_tools.append(entry)
            continue
        # Case 2: single function wrapper or bare schema.
        if isinstance(raw_schema, dict) and raw_schema:
            if "function" in raw_schema and isinstance(raw_schema["function"], dict):
                agent_tools.append(raw_schema)
            else:
                agent_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["external_id"],
                        "description": tool.get("description") or "",
                        "parameters": raw_schema,
                    },
                })
        else:
            # No schema — register by name with empty params so the model
            # at least knows the tool exists.
            agent_tools.append({
                "type": "function",
                "function": {
                    "name": tool["external_id"],
                    "description": tool.get("description") or "",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
    # When the audited agent declares knowledge search capability, expose the
    # local Acme policy search fixture as a callable function. Without it the
    # model has no retrieval path and every retrieval.required expectation
    # fails deterministically — even though the agent's capabilities say it
    # can search its knowledge base.
    if (agent_snapshot.get("capabilities") or {}).get("knowledge_search"):
        kb_names = sorted(
            str(kb.get("name")) for kb in agent_snapshot.get("knowledge_bases") or []
            if isinstance(kb, dict) and kb.get("name")
        )
        kb_hint = f" Sources: {', '.join(kb_names)}." if kb_names else ""
        agent_tools.append({
            "type": "function",
            "function": {
                "name": "acme_search_knowledge",
                "description": (
                    "Search the attached policy knowledge base for relevant "
                    f"passages.{kb_hint} Use this before answering any "
                    "policy, return-window, or eligibility question."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language search query."},
                        "top_k": {"type": "integer", "description": "Maximum passages to return (default 3)."},
                    },
                    "required": ["query"],
                },
            },
        })
    # Both traced and untraced runs inject the tools as OpenAI function
    # definitions: the base model emits native tool_calls and Studio executes
    # them locally (the OWUI compat endpoint does not run an agent pipeline).
    if agent_tools:
        existing = target_params.get("tools") or []
        seen = {t.get("function", {}).get("name") for t in existing if isinstance(t, dict)}
        merged = list(existing) + [t for t in agent_tools if t["function"]["name"] not in seen]
        if merged:
            target_params["tools"] = merged
    # Traced runs route through the OWUI compat endpoint (which exports OTLP
    # spans) instead of the base connection directly. ``_owui_route`` carries
    # the frozen target's OWUI connection so build_model_auditor can override
    # base_url/api_key while keeping the base model's bare model_id — the
    # compat endpoint only resolves "<conn id>.<model id>" for provider models
    # and 401s on agent workspace-model ids.
    _target = target or {}
    if via_wrapper and _target.get("connection_id") and "/chat/api/v1" in (_target.get("base_url") or ""):
        result["_owui_route"] = {"connection_id": int(_target["connection_id"])}
    if target_params:
        result["target_params"] = target_params
    return result


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
    if gen.get("_agent_target"):
        # The agent's generation params (injected tools, temperature, ...) live
        # in ``generation["target_params"]`` and are merged back into the
        # returned kwargs at the bottom of this function. Drop only the
        # provider-only reasoning control here — OWUI's compat endpoint does
        # not accept it. An empty result must stay a dict so that merge has a
        # base to write into.
        target_cfg["gen_params"] = {
            key: value
            for key, value in (target_cfg["gen_params"] or {}).items()
            if key != "reasoning_effort"
        }
        # When the agent's base model differs from the OWUI wrapper endpoint,
        # execute against the base model directly. The frozen agent snapshot
        # carries the resolved base_model connection; use it to override the
        # wrapper's base_url/api_key/model so the target actually runs.
        agent_snap = gen.get("_agent_base_model") or {}
        if agent_snap.get("connection_id") and agent_snap.get("model_id"):
            from model_registry.models import ModelConnection, RegisteredModel
            try:
                base_conn = ModelConnection.objects.get(id=agent_snap["connection_id"])
                base_model_row = RegisteredModel.objects.filter(
                    connection=base_conn, model_id=agent_snap["model_id"]
                ).first()
                if base_model_row:
                    raw_params = dict(base_model_row.default_parameters or {})
                    bp_gen, bp_kw = _split_endpoint_params(raw_params)
                    provider = _normalize_provider(
                        base_conn.provider, base_conn.base_url
                    )
                    # Resolve the API key from the base connection (not the
                    # wrapper's secret_reference which points at OWUI).
                    from model_registry.services import connection_api_key
                    base_key = connection_api_key(base_conn) or "no-auth"
                    # Merge base-model params over the existing gen_params so
                    # that injected agent tools / extra_body survive the override.
                    merged_gen = dict(bp_gen or {})
                    for _k, _v in (target_cfg.get("gen_params") or {}).items():
                        if _k not in merged_gen:
                            merged_gen[_k] = _v
                    target_cfg.update({
                        "model": base_model_row.model_id,
                        "provider": provider,
                        "base_url": base_conn.base_url,
                        "api_key": base_key,
                        "kwargs": {**_client_defaults(provider), **(bp_kw or {})} or None,
                        "gen_params": merged_gen or None,
                    })
                    # Traced runs route through the OWUI compat endpoint so the
                    # W3C traceparent is adopted and exported as OTLP spans.
                    # The base model's bare model_id must be prefixed with the
                    # *base* connection's OWUI prefix ("<conn>.<model>") — that
                    # is how the compat endpoint resolves provider models; the
                    # wrapper connection's prefix (and agent workspace-model
                    # ids like studio.agent-*) are not resolvable there.
                    owui_route = gen.get("_owui_route") or {}
                    if owui_route.get("connection_id"):
                        try:
                            owui_conn = ModelConnection.objects.get(id=owui_route["connection_id"])
                            target_cfg.update({
                                "model": f"{base_conn.id}.{base_model_row.model_id}",
                                "base_url": owui_conn.base_url,
                                # Placeholder; TraceContextModelTarget swaps it
                                # for an OWUI session JWT on every turn.
                                "api_key": "owui-session",
                            })
                        except Exception as _exc:  # noqa: BLE001
                            import logging
                            logging.getLogger(__name__).debug(
                                "OWUI route override failed, using base connection: %s", _exc
                            )
            except Exception as _exc:  # noqa: BLE001 - fall back to wrapper endpoint
                import logging
                logging.getLogger(__name__).debug(
                    "Agent base-model override failed, using wrapper: %s", _exc
                )
    auditor_cfg = _auditor_kwargs_from_snapshot(auditor, resolve_key)
    judge_cfg = _auditor_kwargs_from_snapshot(judge, resolve_key)
    # ``tools`` is a request-shaping parameter for the TARGET role (it makes
    # the model emit tool calls). It is meaningless for auditor/judge roles
    # and some OpenAI-compatible gateways reject it outright (HTTP 400), so
    # drop it from their generation params; per-role overrides (auditor_params
    # / judge_params) can still add tools back explicitly.
    for _cfg in (auditor_cfg, judge_cfg):
        if isinstance(_cfg.get("gen_params"), dict):
            _cfg["gen_params"].pop("tools", None)
            if not _cfg["gen_params"]:
                _cfg["gen_params"] = None

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
    # ``extra_body`` is a per-request parameter (session_id / tool_ids for
    # Open WebUI's saved-chat path), not a ModelAuditor/AsyncOpenAI
    # constructor argument. Pop it out of the merged target param dicts and
    # hand it back to the caller, which injects it into the auditor's
    # ``target_params`` where TraceContextModelTarget reads it per request.
    extra_body = None
    for _pk in ("params", "target_params"):
        _p = kwargs.get(_pk)
        if isinstance(_p, dict) and "extra_body" in _p:
            extra_body = {**(extra_body or {}), **_p.pop("extra_body")}
    if extra_body is not None:
        kwargs["extra_body"] = extra_body
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

    _kw, language = auditor_kwargs(target=target, auditor=auditor, judge=judge, generation=generation)
    extra_body = _kw.pop("extra_body", None)
    try:
        instance = ModelAuditor(**_kw)
    except Exception as exc:
        raise EngineError(f"Failed to construct ModelAuditor: {type(exc).__name__}: {exc}") from exc
    # SimpleAudit 0.3.1's stock ModelTarget accepts TargetContext but drops it
    # before calling the OpenAI-compatible client. Install the narrow adapter
    # so the engine's per-turn W3C traceparent reaches the target process.
    from infra.trace_target import install_trace_context_target

    install_trace_context_target(instance)
    # ``extra_body`` (session_id / tool_ids for Open WebUI's saved-chat path)
    # is a per-request parameter. The engine merges self.target_params over
    # its own base params on every turn, so injecting it here is what makes
    # TraceContextModelTarget route agent turns through OWUI's server-side
    # tool-execution chat.
    if extra_body:
        tp = dict(instance.target_params or {})
        tp["extra_body"] = {**(tp.get("extra_body") or {}), **extra_body}
        instance.target_params = tp
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


# Quiet period that ends settling: longer than the 5s OTLP exporter interval.
_SETTLE_QUIET_S = 6.0


def _collect_evidence_spans(
    correlation: Any,
    provider: Any,
    *,
    token_budget: int | None = None,
    window: tuple[float, float] | None = None,
    settle_timeout: float | None = None,
    capture_level: str = "structural",
    structured: bool = False,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Collect + select evidence spans for the traces a run recorded.

    The engine records ``turn_id -> trace_id`` in ``correlation`` as it
    propagates the W3C ``traceparent``. After the run we fetch the spans for
    every recorded trace id from ``provider`` (the provider owns its backend's
    eventual consistency — it retries until the trace is available or its
    timeout elapses), de-duplicate, and select the evidence-relevant kinds via
    the engine's ``select_spans``.

    When the provider supports it and no trace id matched, a time-window
    fallback (``window`` = wall-clock seconds the run executed) attributes the
    spans the target exported during that window — the correlation fallback
    for targets that do not adopt the propagated trace id.

    The fetch is settled: remote exporters flush in batches, so the spans for
    a trace id can arrive several seconds after the model call returns. The
    span set is re-polled until no new span arrives for ``_SETTLE_QUIET_S``,
    bounded by ``settle_timeout`` (``settle_timeout=None`` disables settling —
    the fast path for tests).

    Returns the legacy span list by default, or a dict with trace evidence tiers
    when ``structured=True``. None is returned when
    there is no trace evidence (caller judges on conversation alone, the normal
    black-box path). A fetch failure is logged, never raised: tracing is
    best-effort evidence and must not fail an audit run.
    """
    if correlation is None or provider is None:
        return None
    import time as _time

    from simpleaudit.tracing.selection import select_spans

    all_spans: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(spans: list[dict[str, Any]] | None) -> None:
        for span in spans or []:
            sid = span.get("span_id")
            if sid in seen:
                continue
            seen.add(sid)
            all_spans.append(span)

    def _fetch_all() -> None:
        for tid in correlation.all_trace_ids():
            try:
                _add(provider.fetch(tid))
            except Exception as exc:  # noqa: BLE001 - tracing must not break the run
                logger = __import__("logging").getLogger("simpleaudit.engine")
                logger.warning("Trace fetch failed for %s: %s", tid, exc)

    def _fetch_window() -> None:
        if window is not None and hasattr(provider, "fetch_window"):
            start_ts, end_ts = window
            try:
                _add(provider.fetch_window(start_ts, end_ts))
            except Exception as exc:  # noqa: BLE001 - tracing must not break the run
                logger = __import__("logging").getLogger("simpleaudit.engine")
                logger.warning("Trace window fetch failed: %s", exc)

    # Settle: remote exporters flush spans in batches on a fixed interval (5s
    # for OpenWebUI's OTLP exporter), so the last batch of a run can arrive
    # several seconds after the model call returns. Keep polling until no new
    # span has arrived for a full export interval, bounded by ``settle_timeout``.
    if settle_timeout:
        deadline = _time.monotonic() + settle_timeout
        while True:
            _fetch_all()
            if not all_spans:
                _fetch_window()
            if all_spans:
                before = len(all_spans)
                _time.sleep(min(_SETTLE_QUIET_S, max(0.0, deadline - _time.monotonic())))
                _fetch_all()
                if len(all_spans) == before:
                    break
            elif _time.monotonic() >= deadline:
                break
            else:
                _time.sleep(0.25)
            if _time.monotonic() >= deadline:
                break

    if not all_spans:
        return None
    from audits.agentic.privacy import apply_content_capture_level

    # Redact once at the requested level; re-applying a stricter level is a
    # no-op over already-sanitized spans (content keys are simply absent).
    all_spans = apply_content_capture_level(_drop_connection_noise(all_spans), capture_level)
    # ``selector_version`` was added after the first public tracing release;
    # keep Studio compatible with both the released engine and newer git builds.
    try:
        selection_result = select_spans(
            all_spans, token_budget=token_budget, selector_version="v1"
        )
    except TypeError as exc:
        if "selector_version" not in str(exc):
            raise
        selection_result = select_spans(all_spans, token_budget=token_budget)
    selected_spans = selection_result.selected or []
    if not selected_spans:
        return None

    if not structured:
        return all_spans

    return {
        "trace_ids": correlation.all_trace_ids(),
        # Full captured span set (de-duplicated, capture-level sanitized) so the
        # agentic lane can evaluate every span of the audited invocation, not
        # just the selection's evidence subset. ``all_span_refs`` stays as the
        # compact index; ``all_spans`` is the complete payload.
        "all_spans": all_spans,
        "all_span_refs": [
            {"span_id": s.get("span_id"), "name": s.get("name"), "kind": s.get("kind")}
            for s in all_spans
        ],
        "selected_spans": selected_spans,
        "selection": {
            "policy_version": "v1",
            "selector_version": getattr(selection_result, "selector_version", "v1"),
            "selected_count": len(selection_result.selected or []),
            "elided_count": selection_result.elided_count,
            "budget": selection_result.budget,
        },
        "diagnostics": {},
    }


def _drop_connection_noise(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop connection-level spans (connect / DNS / TLS / sub-ms GETs) that
    carry no judging signal.

    Plain OTel auto-instrumentation (e.g. an OpenWebUI deployment) exports
    hundreds of these around each request; keeping them would drown the real
    model calls in both the judge's evidence and the UI trace card. Only
    applied when something survives, so a target that exports nothing else
    still yields its spans.
    """
    def _noise(s: dict[str, Any]) -> bool:
        name = (s.get("name") or "").lower()
        if name in {"connect", "dns lookup", "tls handshake"}:
            return True
        start, end = s.get("start_time"), s.get("end_time")
        try:
            dur = (float(end) - float(start)) * 1000.0 if end and start else -1.0
        except (TypeError, ValueError):
            dur = -1.0
        return name == "get" and 0 <= dur < 1.0

    kept = [s for s in spans if not _noise(s)]
    return kept or list(spans)


def _attach_final_response(payload: dict[str, Any]) -> None:
    """Surface the last assistant turn as ``judgment["final_response"]``.

    The agentic semantic judge's evidence packet needs the response it is
    judging; without it the packet carries an empty final_response and the
    judge can only reason from spans. The conversation on the result payload
    is authoritative (it is what the target actually said).
    """
    conversation = payload.get("conversation")
    if not isinstance(conversation, list):
        return
    for turn in reversed(conversation):
        if isinstance(turn, dict) and turn.get("role") == "assistant":
            content = turn.get("content")
            if isinstance(content, str) and content.strip():
                judgment = payload.get("judgment")
                if not isinstance(judgment, dict):
                    judgment = {}
                judgment.setdefault("final_response", content)
                payload["judgment"] = judgment
                return


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
    trace_config: dict | None = None,
) -> dict[str, Any]:
    """Execute one scenario through the real engine and return a serializable result.

    Runs the async ``ModelAuditor.run_async`` (single scenario) to completion
    and returns ``AuditResult.to_dict()`` plus the language used. Raises
    ``EngineError`` on load failure; a mid-conversation/judging failure is
    captured by the engine itself as a severity of ``ERROR`` in the returned
    dict (not raised), matching the engine's own error-handling contract.

    If ``on_turn`` is provided, it is called at each phase boundary with
    ``(turn_index, max_turns, role)`` where role is "auditor", "target", or
    "judge". NOTE: on_turn is called from within the asyncio event loop — do
    NOT perform blocking I/O (e.g. Django ORM) inside it.

    Tracing (best-effort, Promptfoo parity): when ``trace_config`` is given, a
    provider is built from it (``builtin`` OTLP receiver or ``tempo`` fetch) and
    a fresh ``TraceCorrelation`` is passed to the engine, which propagates a W3C
    ``traceparent`` per turn and records ``turn_id -> trace_id``. After target
    execution, the evidence resolver fetches spans for the recorded trace ids,
    selects them, and passes them to the judge. The selected spans are also
    attached to the result under ``judgment["evidence_spans"]`` for post-hoc use.
    """
    auditor_instance, language = build_model_auditor(
        target=target, auditor=auditor, judge=judge, generation=generation,
    )
    scenario = scenario_dict(
        name=name, description=description, expected_behavior=expected_behavior, test_prompt=test_prompt,
        severity_ceiling=severity_ceiling, documents=documents, file_uri=file_uri, category=category,
        metadata=metadata,
    )

    provider = None
    correlation = None
    audit_run_id = None
    evidence_resolver = None
    _resolver_agentic_result: dict[str, Any] = {}  # populated by resolver for agentic scenarios
    if trace_config:
        from simpleaudit.tracing.context import TraceCorrelation, new_trace_id

        from infra.tracing import build_trace_provider

        provider = build_trace_provider(trace_config)
        if provider is not None:
            audit_run_id = f"audit_{new_trace_id()[:12]}"
            correlation = TraceCorrelation(audit_run_id=audit_run_id)

            # Supply evidence resolver to the engine. It runs after target
            # execution, before judge, and has access to correlation IDs.
            # For agentic scenarios, it also runs deterministic checks so the
            # judge sees evaluated execution facts alongside raw spans.
            _agentic_meta = (metadata or {}).get("agentic")
            # Agentic guidance: judge-level option takes priority over scenario metadata.
            _judge_spec = (judge.get("judge") or {}).get("spec") or {}
            _agentic_guidance = (
                (_judge_spec.get("options") or {}).get("agentic_guidance")
                or (_agentic_meta or {}).get("judge_guidance")
                or None
            )
            import time as _t
            _exec_start_ts = _t.time()

            def _evidence_resolver(execution):
                # Window: from just before execution started to now (plus a
                # small buffer for late-arriving spans).  Enables the
                # fetch_window fallback for targets that don't propagate
                # the W3C traceparent (e.g. OpenWebUI).
                _window = (_exec_start_ts - 2.0, _t.time() + 5.0)
                evidence = _collect_evidence_spans(
                    execution.trace_correlation, provider, settle_timeout=20.0,
                    window=_window,
                    capture_level=(trace_config or {}).get("content_capture", "structural"),
                    structured=True,
                )
                selected = (evidence or {}).get("selected_spans") if evidence else None
                if not _agentic_meta:
                    return selected
                # Agentic scenario: run deterministic checks on the captured
                # spans and return a combined dict so the judge gets both raw
                # traces and structured check results in one prompt.
                # SKIP entirely when there are no real OTLP spans — agentic
                # audit requires actual tool/retrieval traces, not conversation
                # text synthesized into fake "inference" steps.
                try:
                    from audits.agentic.evaluate import evaluate as evaluate_agentic
                    from audits.agentic.expectations import validate_agentic_metadata
                    from audits.agentic.trajectory import normalize
                    from audits.agentic.verdict_policy import compute_overall_verdict

                    all_spans = (evidence or {}).get("all_spans") or []
                    conv = list(execution.conversation) if hasattr(execution, "conversation") else []
                    # When no real OTLP spans exist, fall back to the
                    # conversation-derived trajectory (parses [TOOL_CALL]
                    # markers). This lets the judge evaluate actual agent
                    # behaviour even when the target doesn't export traces.
                    trajectory = normalize(all_spans, conversation=conv or None)
                    expectations = validate_agentic_metadata(metadata or {})

                    evaluation = evaluate_agentic(
                        trajectory, expectations, None,
                        llm=None, task=test_prompt or "",
                    )
                    complete = bool(trajectory.steps)
                    derived_from_conv = not all_spans and complete
                    evaluation["trace_completeness"] = {
                        "complete": complete,
                        "derived_from_conversation": derived_from_conv,
                        "span_count": len(trajectory.steps),
                        "captured_kinds": sorted({step.kind for step in trajectory.steps}),
                        "missing_reason": (
                            "Derived from conversation markers (no OTLP spans captured)."
                            if derived_from_conv
                            else (None if complete else "No trace spans were captured.")
                        ),
                    }
                    if not complete:
                        evaluation.update(compute_overall_verdict({
                            "missing_trace": True,
                            "deterministic_fail": any(c.get("status") == "FAIL" for c in evaluation.get("checks", [])),
                        }))
                    # Mark semantic as deferred (needs LLM; worker handles post-hoc).
                    semantic_cfg = _agentic_meta.get("semantic_judge") or {}
                    if semantic_cfg.get("enabled") and semantic_cfg.get("criteria"):
                        evaluation["semantic_judgement"] = {
                            "status": "DEFERRED",
                            "rationale": "Semantic dimensions evaluated post-hoc by worker.",
                            "dimensions": {},
                        }
                    _resolver_agentic_result.clear()
                    _resolver_agentic_result.update(evaluation)
                    return {
                        "selected_spans": selected,
                        "agentic_evaluation": evaluation,
                        "agentic_guidance": _agentic_guidance,
                    }
                except Exception:
                    import logging
                    logging.getLogger("infra.engine").warning(
                        "Agentic evaluation in evidence resolver failed; falling back to spans only",
                        exc_info=True,
                    )
                    return selected
            evidence_resolver = _evidence_resolver

    full_trace_evidence = None
    import time as _t
    _exec_start_ts_outer = _t.time()
    try:
        with (provider or nullcontext()):
            # run_async maps the scenario dict onto run_scenario (file_uri,
            # documents, judge notes, the scenario facts a judge's
            # post-processor reads). When tracing, the engine propagates the
            # traceparent per turn, records the trace ids in correlation, and
            # calls the evidence_resolver after target execution but before judge.
            # Close the auditor's async HTTP clients inside this loop; left to
            # GC they'd try to close transports bound to the closed loop.
            from infra.trace_target import run_with_client_cleanup

            results = asyncio.run(
                run_with_client_cleanup(
                    lambda: auditor_instance.run_async(
                        [scenario],
                        language=language,
                        on_turn=on_turn,
                        audit_run_id=audit_run_id,
                        trace_correlation=correlation,
                        evidence_resolver=evidence_resolver,
                    ),
                    [auditor_instance],
                )
            )
            # Collect full evidence tiers after run for persistence. The resolver
            # above only passes selected_spans to the judge; this captures all
            # tiers (trace_ids, all_spans, selected_spans, selection metadata).
            if provider is not None and correlation is not None:
                full_trace_evidence = _collect_evidence_spans(
                    correlation, provider, settle_timeout=20.0,
                    window=(_exec_start_ts_outer - 2.0, _t.time() + 5.0),
                    capture_level=(trace_config or {}).get("content_capture", "structural"),
                    structured=True,
                )
    except Exception as exc:
        raise EngineError(f"Scenario execution crashed: {type(exc).__name__}: {exc}") from exc

    payload = results[0].to_dict()
    if full_trace_evidence:
        # Persist full evidence tiers separately. For backward compatibility,
        # also populate judgment.evidence_spans with selected_spans.
        payload["trace_evidence"] = full_trace_evidence
        judgment = payload.get("judgment")
        if not isinstance(judgment, dict):
            judgment = {}
        judgment["evidence_spans"] = full_trace_evidence.get("selected_spans") or []
        payload["judgment"] = judgment
        # The agentic semantic judge needs the agent's actual final response
        # (the last assistant turn) as ground truth for its evidence packet;
        # the engine's AuditResult.to_dict() does not surface it.
        _attach_final_response(payload)
    # Attach agentic evaluation from the evidence resolver (deterministic
    # checks ran pre-judge; semantic dimensions are deferred to the worker).
    if _resolver_agentic_result:
        payload["agentic_evaluation"] = _resolver_agentic_result
    if correlation is not None:
        # The W3C trace ids the engine propagated this scenario's turns under;
        # persisted on the ScenarioResult so the UI can re-fetch the spans by
        # id (and show which traces the run covered) even after the evidence
        # selection has narrowed them.
        payload["trace_ids"] = correlation.all_trace_ids()
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
    severity_ceiling: str = "",
    documents: list | None = None,
    file_uri=None,
    category: str = "",
    metadata: dict | None = None,
    trace_config: dict | None = None,
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

    Tracing (best-effort, Promptfoo parity): when ``trace_config`` is given, a
    provider is built from it and a fresh ``TraceCorrelation`` is shared across
    all reps. Each rep records its ``turn_id -> trace_id`` links, and after the
    run the spans for every recorded trace id are fetched, selected, and
    attached to each rep's result under ``judgment["evidence_spans"]`` (de-
    duplicated per rep). The engine judges on the conversation; trace evidence
    is attached post-run, matching the single-rep path.
    """
    _ensure_engine_available()
    try:
        from simpleaudit.experiment import AuditExperiment
    except Exception as exc:
        raise EngineError(f"Failed to import SimpleAudit AuditExperiment: {exc}") from exc

    # AuditExperiment constructs a fresh ModelAuditor internally for every
    # repetition. Replace that module-local class with the same adapter-aware
    # subclass used by the single-repetition path.
    import simpleaudit.experiment as experiment_module

    from infra.trace_target import TraceContextModelAuditor

    experiment_module.ModelAuditor = TraceContextModelAuditor

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
    # ``extra_body`` is a per-request parameter (session_id / tool_ids for
    # Open WebUI's saved-chat path), not a ModelAuditor constructor argument.
    # TraceContextModelAuditor.__init__ pops it and injects it into its own
    # target_params so the engine merges it into every turn's request params.
    extra_body = kwargs.pop("extra_body", None)
    model_entry = {k: v for k, v in kwargs.items() if v is not None}
    if extra_body is not None:
        model_entry["extra_body"] = extra_body
    model_entry["label"] = f"{kwargs['model']} (platform)"

    # Tracing: build a provider + a per-rep TraceCorrelation. Each rep gets its
    # own correlation (assigned at the rep boundary) so its turn->trace links
    # are attributable to that rep; evidence is fetched per rep after the run.
    provider = None
    rep_correlations: dict[int, Any] = {}
    if trace_config:
        from simpleaudit.tracing.context import new_trace_id

        from infra.tracing import build_trace_provider

        provider = build_trace_provider(trace_config)
        if provider is not None:
            audit_run_id = f"audit_{new_trace_id()[:12]}"
            # Stash on the provider so the rep-boundary callback can mint a
            # fresh correlation per rep without extra plumbing.
            provider._audit_run_id = audit_run_id

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
    # starts and (when tracing) mint a fresh per-rep TraceCorrelation. Must be
    # async-safe (no ORM calls).
    def _rep_is_done(label: str, rep_index: int) -> bool:
        if provider is not None:
            from simpleaudit.tracing.context import TraceCorrelation

            rep_correlations[rep_index] = TraceCorrelation(
                audit_run_id=getattr(provider, "_audit_run_id", None)
            )
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

    # Mutable holder the engine reads each rep: the per-rep correlation
    # assigned at the rep boundary. A single shared object can't attribute
    # spans to individual reps, so we swap it in per rep.
    current_correlation: dict[str, Any] = {"corr": None}

    def _tracing_correlation() -> Any:
        return current_correlation["corr"]

    # Evidence resolver for agentic multi-rep runs: uses the per-rep
    # correlation to fetch spans and run deterministic checks pre-judge.
    _agentic_meta_rep = (metadata or {}).get("agentic")
    _resolver_agentic_results: list[dict[str, Any]] = []  # per-rep results
    if _agentic_meta_rep and provider is not None:
        def _rep_evidence_resolver(execution):
            corr = current_correlation["corr"]
            evidence = _collect_evidence_spans(
                corr, provider, settle_timeout=20.0,
                capture_level=(trace_config or {}).get("content_capture", "structural"),
                structured=True,
            )
            selected = (evidence or {}).get("selected_spans") if evidence else None
            try:
                from audits.agentic.evaluate import evaluate as evaluate_agentic
                from audits.agentic.expectations import validate_agentic_metadata
                from audits.agentic.trajectory import normalize
                from audits.agentic.verdict_policy import compute_overall_verdict

                all_spans = (evidence or {}).get("all_spans") or []
                conv = list(execution.conversation) if hasattr(execution, "conversation") else []
                trajectory = normalize(all_spans, conversation=conv or None)
                expectations = validate_agentic_metadata(metadata or {})
                evaluation = evaluate_agentic(
                    trajectory, expectations, None,
                    llm=None, task=test_prompt or "",
                )
                complete = bool(trajectory.steps)
                evaluation["trace_completeness"] = {
                    "complete": complete,
                    "span_count": len(trajectory.steps),
                    "captured_kinds": sorted({step.kind for step in trajectory.steps}),
                    "missing_reason": None if complete else "No trace spans were captured.",
                }
                if not complete:
                    evaluation.update(compute_overall_verdict({
                        "missing_trace": True,
                        "deterministic_fail": any(c.get("status") == "FAIL" for c in evaluation.get("checks", [])),
                    }))
                semantic_cfg = _agentic_meta_rep.get("semantic_judge") or {}
                if semantic_cfg.get("enabled") and semantic_cfg.get("criteria"):
                    evaluation["semantic_judgement"] = {
                        "status": "DEFERRED",
                        "rationale": "Semantic dimensions evaluated post-hoc by worker.",
                        "dimensions": {},
                    }
                _resolver_agentic_results.append(evaluation)
                return {
                    "selected_spans": selected,
                    "agentic_evaluation": evaluation,
                    "agentic_guidance": (_agentic_meta_rep.get("judge_guidance") or None),
                }
            except Exception:
                import logging
                logging.getLogger("infra.engine").warning(
                    "Agentic eval in multi-rep resolver failed; spans only", exc_info=True,
                )
                return selected
    else:
        _rep_evidence_resolver = None

    try:
        with (provider or nullcontext()):
            # AuditExperiment builds a fresh TraceContextModelAuditor per rep;
            # the cleanup wrapper captures each one and closes its async HTTP
            # clients before asyncio.run() closes the loop that owns them.
            from infra.trace_target import run_with_client_cleanup

            results = asyncio.run(
                run_with_client_cleanup(
                    lambda: experiment.run_scenario_reps(
                        model_index=0, scenario=scenario, max_turns=max_turns, language=language,
                        on_turn=on_turn,
                        audit_run_id=getattr(provider, "_audit_run_id", None) if provider else None,
                        trace_correlation=_tracing_correlation,
                        evidence_resolver=_rep_evidence_resolver,
                    ),
                    [experiment],
                )
            )
            # Collect per-rep evidence tiers while the provider is still alive.
            if provider is not None:
                for idx, rep in enumerate(reps):
                    corr = rep_correlations.get(rep.get("_rep_index"))
                    full_trace_evidence = _collect_evidence_spans(
                        corr, provider, settle_timeout=20.0, structured=True,
                    )
                    if full_trace_evidence:
                        # Persist full evidence tiers separately. For backward compatibility,
                        # also populate judgment.evidence_spans with selected_spans.
                        rep["trace_evidence"] = full_trace_evidence
                        judgment = rep.get("judgment")
                        if not isinstance(judgment, dict):
                            judgment = {}
                        judgment["evidence_spans"] = full_trace_evidence.get("selected_spans") or []
                        rep["judgment"] = judgment
                        _attach_final_response(rep)
                    if corr is not None:
                        rep["trace_ids"] = corr.all_trace_ids()
                    # Attach per-rep agentic evaluation from the resolver.
                    if idx < len(_resolver_agentic_results):
                        rep["agentic_evaluation"] = _resolver_agentic_results[idx]
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

    # Newer SimpleAudit releases expose this helper; older supported releases
    # do not, so retain the same modal shape locally for compatibility.
    severities = [rep.get("severity", "") for rep in reps]
    try:
        from simpleaudit.repeated_results import aggregate_severities
    except ImportError:
        from collections import Counter

        counts = Counter(severities)
        ordered = {"pass": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        mode_count = max(counts.values(), default=0)
        candidates = [severity for severity, count in counts.items() if count == mode_count]
        most_common = max(candidates, key=lambda value: ordered.get(value, -1), default="")
        agg = {
            "most_common_severity": most_common,
            "agreement_rate": mode_count / len(severities) if severities else 0.0,
            "severity_distribution": dict(counts),
        }
    else:
        agg = aggregate_severities(severities)

    return {
        "reps": reps,
        "aggregated_severity": agg["most_common_severity"],
        "agreement_rate": round(agg["agreement_rate"], 4),
        "severity_distribution": agg["severity_distribution"],
        "n_repetitions": len(reps),
        "_language": language,
    }


def audit_result_fields() -> set[str]:
    """The field names of the engine's ``AuditResult`` dataclass.

    Computed lazily so importing this module never requires the engine to be
    installed. Used by callers that must separate engine-owned keys from
    Studio-specific extras before/after :func:`build_audit_results_export`.
    """
    _ensure_engine_available()
    import dataclasses

    from simpleaudit.results import AuditResult

    return {f.name for f in dataclasses.fields(AuditResult)}


def build_audit_results_export(payloads: list[dict[str, Any]], *, timestamp: str | None = None) -> dict[str, Any]:
    """Assemble the engine's ``AuditResults.to_dict()`` document from stored results.

    ``payloads`` are persisted per-scenario ``AuditResult.to_dict()`` records. Keys
    the engine's ``AuditResult`` does not define (Studio bookkeeping such as
    ``trace_ids`` or ``_language``) are dropped, and the summary block is computed
    by the engine itself, so the document keeps the engine's own shape and loads
    back with ``AuditResults.load``. This is the only place Studio may import the
    engine for export assembly.
    """
    fields = audit_result_fields()
    from simpleaudit.results import AuditResult, AuditResults

    collection = AuditResults([AuditResult(**{k: v for k, v in p.items() if k in fields}) for p in payloads])
    if timestamp:
        collection.timestamp = timestamp
    return collection.to_dict()
