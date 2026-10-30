"""SimpleAudit target adapter that preserves per-turn trace context.

SimpleAudit 0.3.1 exposes ``TargetContext.trace_headers`` but its default
``ModelTarget`` does not forward that context to the OpenAI-compatible client.
This adapter keeps the normal model target behavior while adding the W3C
headers to the target request.
"""
from __future__ import annotations

import contextvars
import inspect
import json
import logging
from typing import Any
from uuid import uuid4

from simpleaudit.targets.base import TargetContext, TargetResponse
from simpleaudit.targets.model import ModelTarget


def _chunk_value(value: Any, key: str, default: Any = None) -> Any:
    """Read an OpenAI-compatible stream field from an object or mapping."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _saved_chat_content(message: dict[str, Any]) -> str:
    """Extract plain assistant text from an Open WebUI saved-chat message."""
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    for item in message.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                content = (content or "") + text
    return content or ""


# Cached OWUI JWT token (avoid re-authenticating on every turn).
_owui_jwt_cache: dict[str, Any] = {"token": None, "expires": 0.0}


async def _owui_get_jwt(base_url: str) -> str | None:
    """Obtain a JWT from OWUI's trusted-header signin endpoint."""
    import time

    now = time.monotonic()
    if _owui_jwt_cache["token"] and _owui_jwt_cache["expires"] > now:
        return _owui_jwt_cache["token"]

    import httpx

    # The base_url is like http://127.0.0.1:8080/api/v1 or
    # http://127.0.0.1:8080/chat/api/v1 — we need the origin for /api/v1/auths/signin
    origin = base_url.rsplit("/api/v1", 1)[0].rstrip("/")
    url = f"{origin}/api/v1/auths/signin"
    headers = {
        "Content-Type": "application/json",
        "X-Studio-Email": "admin@example.local",
        "X-Studio-Name": "Admin",
        "X-Studio-Role": "admin",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"email": "admin@example.local", "password": ""}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token")
            if token:
                _owui_jwt_cache["token"] = token
                # OWUI default JWT expiry is 4 weeks; cache for 1 hour to be safe.
                _owui_jwt_cache["expires"] = now + 3600.0
            return token
    except Exception:
        logging.getLogger(__name__).warning("Failed to obtain OWUI JWT from %s", url, exc_info=True)
        return None


async def _owui_internal_agent_call(
    *,
    base_url: str,
    model: str,
    system: str | None,
    user: str,
    history: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    kb_ids: list[str] | None,
    params: dict[str, Any],
    max_retries: int = 2,
    retry_backoff: float = 2.0,
) -> tuple[str, int, int]:
    """Call OWUI's internal /api/chat/completions with KB files + tools.

    This path gives us real KB retrieval (via ``files``), real function
    calling (via ``tools``), and OTLP traces (OWUI exports spans to
    Studio's /otlp/v1/traces endpoint).

    Returns (content, input_tokens, output_tokens). Tool calls are
    serialized into content as [TOOL_CALL name(args)] markers, same
    format the streaming path produces.
    """
    import asyncio

    import httpx

    jwt = await _owui_get_jwt(base_url)
    if not jwt:
        raise RuntimeError("Cannot authenticate with Open WebUI internal API")

    origin = base_url.rsplit("/api/v1", 1)[0].rstrip("/")
    url = f"{origin}/api/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt}",
        "X-Studio-Email": "admin@example.local",
        "X-Studio-Name": "Admin",
        "X-Studio-Role": "admin",
    }

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
    if kb_ids:
        body["files"] = [{"type": "collection", "id": kid} for kid in kb_ids]
    # Pass through relevant generation params
    for key in ("temperature", "max_tokens", "top_p"):
        if key in params and params[key] is not None:
            body[key] = params[key]

    attempt = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            break
        except Exception:
            if attempt >= max_retries:
                raise
            await asyncio.sleep(retry_backoff * (2 ** attempt))
            attempt += 1

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "?")
        args = fn.get("arguments", "{}")
        content += f"[TOOL_CALL {name}({args})]"

    usage = data.get("usage") or {}
    return (
        content,
        usage.get("prompt_tokens", 0) or 0,
        usage.get("completion_tokens", 0) or 0,
    )


_OPENWEBUI_AGENT_UNSUPPORTED_PARAMS = frozenset({
    "reasoning_effort",
    "search_mode",
    "top_k",
    "relevance_threshold",
    # The agent's Open Webui model supplies its own tools; Studio's tool specs
    # would make Open Webui expect a caller-side executor.
    "tools",
    "tool_choice",
})


def _openwebui_saved_chat_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remove generation fields rejected by Open WebUI's Agent endpoint.

    Agent retrieval settings are already frozen into the Open WebUI workspace
    model.  The saved-chat endpoint rejects those settings, and provider-only
    reasoning controls, as unknown request arguments.
    """
    filtered = {
        key: value for key, value in params.items()
        if key not in _OPENWEBUI_AGENT_UNSUPPORTED_PARAMS
    }
    if isinstance(filtered.get("extra_body"), dict):
        filtered["extra_body"] = _openwebui_saved_chat_params(filtered["extra_body"])
    return filtered


async def _openwebui_saved_chat_call(
    client: Any,
    model: str,
    *,
    kwargs: dict[str, Any],
    trace_headers: dict[str, str] | None,
) -> tuple[str, int, int] | None:
    """Run an Agent through Open WebUI's chat-owned tool execution path.

    Open WebUI's direct compatibility endpoint deliberately leaves native tool
    calls for the caller.  A saved chat creates the server event emitter, which
    is the supported path that executes model-attached tools and knowledge
    retrieval.  Return ``None`` for non-Open-WebUI clients so normal streaming
    remains unchanged for every other provider.
    """
    raw_client = getattr(client, "client", None)
    base_url = str(getattr(raw_client, "base_url", ""))
    extra_body = kwargs.get("extra_body") or {}
    if raw_client is None or "/chat/api/v1" not in base_url:
        return None

    import asyncio

    user_id = str(uuid4())
    assistant_id = str(uuid4())
    messages = kwargs.get("messages") or []
    user_message = next(
        (message for message in reversed(messages) if message.get("role") == "user"),
        {"role": "user", "content": ""},
    )
    body = _openwebui_saved_chat_params(kwargs)
    body.pop("extra_headers", None)
    body.pop("extra_body", None)
    body.update(_openwebui_saved_chat_params(extra_body))
    body = _openwebui_saved_chat_params(body)
    # Open Webui's chat endpoint needs a session to emit tool events on; one per turn.
    body.setdefault("session_id", str(uuid4()))
    body.update(
        {
            "parent_id": None,
            "user_message": {
                "id": user_id,
                "role": "user",
                "content": user_message.get("content", ""),
                "parentId": None,
            },
            "message_ids": [{"model_id": model, "message_id": assistant_id}],
            "stream": True,
        }
    )
    options = {"headers": trace_headers} if trace_headers else {}
    created = await raw_client.post("/chat/completions", cast_to=dict, body=body, options=options)
    chat_id = created.get("chat_id") if isinstance(created, dict) else None
    if not chat_id:
        raise RuntimeError("Open WebUI saved-chat request returned no chat_id")

    # Agent loops with several tool calls (order lookup + knowledge
    # retrieval) can take minutes end-to-end; 300s covers observed OWUI
    # saved-chat durations (up to ~2.5 min per turn in multi-tool runs).
    for _ in range(600):
        await asyncio.sleep(0.5)
        chat = await raw_client.get(f"/chats/{chat_id}", cast_to=dict, options=options)
        chat_payload = chat.get("chat", chat) if isinstance(chat, dict) else {}
        history = chat_payload.get("history", {}) if isinstance(chat_payload, dict) else {}
        messages_by_id = history.get("messages", {}) if isinstance(history, dict) else {}
        if isinstance(messages_by_id, dict):
            message = messages_by_id.get(assistant_id, {})
            if not message:
                assistant_messages = [
                    item for item in messages_by_id.values() if item.get("role") == "assistant"
                ]
                message = assistant_messages[-1] if assistant_messages else {}
        else:
            message = next(
                (item for item in messages_by_id if item.get("id") == assistant_id),
                {},
            )
        if message.get("error"):
            raise RuntimeError(f"Open WebUI chat failed: {message['error']}")
        if message.get("done"):
            usage = message.get("usage") or {}
            return (
                _saved_chat_content(message),
                usage.get("prompt_tokens", 0) or 0,
                usage.get("completion_tokens", 0) or 0,
            )
    raise TimeoutError("Open WebUI saved chat did not complete within 300 seconds")


async def _streaming_call(
    client: Any,
    model: str,
    *,
    system: str | None,
    user: str,
    history: list[dict[str, Any]] | None,
    file_uri: str | list[str] | None,
    documents: list[str | dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
    params: dict[str, Any],
    max_retries: int,
    retry_backoff: float,
) -> tuple[str, int, int]:
    """Consume an OpenAI-compatible stream, including Open WebUI tool loops.

    Open WebUI's server-side tool executor is attached to its streaming
    response middleware.  A non-stream request returns the provider's native
    ``tool_calls`` turn without executing it, so the normal SimpleAudit call
    path cannot drive an Agent with tools or knowledge retrieval.
    """
    # NOTE: OWUI v0.12.0 does NOT expose an internal /api/chat/completions
    # endpoint that runs the agent pipeline (tools + KB retrieval).  The
    # OpenAI-compat endpoint at /api/v1/chat/completions is a thin proxy that
    # bypasses the agent loop entirely.  Therefore we call the agent's base
    # model directly (via the engine's _agent_base_model override) with tools
    # in the standard OpenAI format; Studio executes tool calls locally.
    # The _owui_internal_agent_call helper below is kept for future use once
    # OWUI exposes a proper HTTP agent-execution API.

    from simpleaudit.model_auditor import _expand_documents, _expand_files

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(_expand_files(_expand_documents(message)) for message in history)
    else:
        message: dict[str, Any] = {"role": "user", "content": user}
        if documents:
            message["documents"] = documents
        if file_uri:
            message["file_uri"] = file_uri
        messages.append(_expand_files(_expand_documents(message)))

    kwargs = dict(params)
    kwargs.update(model=model, messages=messages, stream=True)
    if response_format:
        kwargs["response_format"] = response_format

    # ``extra_headers`` (W3C traceparent) is a studio-level parameter that
    # _streaming_call consumes itself for the saved-chat request options —
    # it must never ride along as an OpenAI request field.
    trace_headers = kwargs.pop("extra_headers", None)
    saved_chat_result = await _openwebui_saved_chat_call(
        client,
        model,
        kwargs=kwargs,
        trace_headers=trace_headers,
    )
    if saved_chat_result is not None:
        return saved_chat_result

    def _tc_field(obj: Any, key: str, default: Any = None) -> Any:
        """Read a field from a dict or Pydantic object safely."""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    total_input_tokens = 0
    total_output_tokens = 0
    max_tool_loops = int(params.get("_max_tool_loops", 3))
    tools_spec = params.get("tools") or []
    _tool_executor = _build_tool_executor(tools_spec)

    # --- Tool execution loop --------------------------------------------------
    # When the model returns tool_calls, we execute them locally and send the
    # results back until we get a plain text response or exhaust the budget.
    # Each iteration appends to the local `messages` list so the model sees
    # the full conversation including prior tool results.
    current_messages = list(messages)
    loop_count = 0

    all_tool_markers: list[str] = []  # Accumulated [TOOL_CALL]/[TOOL_RESULT] for trajectory

    while True:
        # Tool-loop requests must not stream: Open WebUI marks the OTLP span
        # ERROR when the client disconnects before the SSE body finishes, and
        # a tool_calls response would make us abandon the stream early. A
        # non-streaming call completes fully server-side so spans stay OK.
        if tools_spec:
            kwargs["stream"] = False
        attempt = 0
        while True:
            try:
                kwargs["messages"] = current_messages
                response = await client.acompletion(**kwargs)
                break
            except Exception:
                if attempt >= max_retries:
                    raise
                import asyncio

                await asyncio.sleep(retry_backoff * (2**attempt))
                attempt += 1

        # Parse the response (non-streaming path — SimulaChat / most providers
        # return a complete JSON body when stream=True is not honoured).
        content_parts: list[str] = []
        tool_call_parts: list[str] = []
        input_tokens = output_tokens = 0
        raw_tool_calls: list[dict[str, Any]] = []

        if not hasattr(response, "__aiter__"):
            choices = _chunk_value(response, "choices", []) or []
            message = _chunk_value(choices[0], "message", {}) if choices else {}
            content = _chunk_value(message, "content", "") or ""
            tcs = _chunk_value(message, "tool_calls") or []
            for tc in tcs:
                fn = _tc_field(tc, "function", {}) or {}
                name = _tc_field(fn, "name", "?")
                args = _tc_field(fn, "arguments", "{}")
                tool_call_parts.append(f"[TOOL_CALL {name}({args})]")
                raw_tool_calls.append({"id": _tc_field(tc, "id", ""), "name": name, "arguments": args})
            usage = _chunk_value(response, "usage")
            input_tokens = _chunk_value(usage, "prompt_tokens", 0) or 0
            output_tokens = _chunk_value(usage, "completion_tokens", 0) or 0
        else:
            # Streaming path
            _streamed_tc: dict[int, list[str]] = {}
            _streamed_tc_meta: dict[int, dict[str, str]] = {}
            async for chunk in response:
                choices = _chunk_value(chunk, "choices", []) or []
                if choices:
                    delta = _chunk_value(choices[0], "delta", {}) or {}
                    content = _chunk_value(delta, "content", "") or ""
                    if content:
                        content_parts.append(content)
                    d_tcs = _chunk_value(delta, "tool_calls") or []
                    for tc in d_tcs:
                        idx = _tc_field(tc, "index", 0) or 0
                        fn = _tc_field(tc, "function", None)
                        meta = _streamed_tc_meta.setdefault(idx, {"id": "", "name": "", "args": ""})
                        tc_id = _tc_field(tc, "id", "")
                        if tc_id:
                            meta["id"] = tc_id
                        if fn is not None:
                            name = _tc_field(fn, "name", "") or ""
                            args = _tc_field(fn, "arguments", "") or ""
                            if name:
                                meta["name"] = name
                            if args:
                                meta["args"] += args
                usage = _chunk_value(chunk, "usage")
                if usage:
                    input_tokens = _chunk_value(usage, "prompt_tokens", 0) or 0
                    output_tokens = _chunk_value(usage, "completion_tokens", 0) or 0
            for idx in sorted(_streamed_tc_meta):
                m = _streamed_tc_meta[idx]
                if m["name"]:
                    raw_tool_calls.append({"id": m["id"], "name": m["name"], "arguments": m["args"] or "{}"})
            # Serialize tool calls into content for the deterministic checker.
            for tc in raw_tool_calls:
                tool_call_parts.append(f"[TOOL_CALL {tc['name']}({tc['arguments']})]")

        total_input_tokens += input_tokens
        total_output_tokens += output_tokens

        # Accumulate tool markers for the full trajectory record.
        all_tool_markers.extend(tool_call_parts)

        # No tool calls → return the final text response with all prior tool markers.
        if not raw_tool_calls:
            final_content = "".join(all_tool_markers) + "".join(content_parts)
            # Retry once on empty completion: some providers (SimulaChat)
            # intermittently return content="" with no tool_calls on
            # multi-turn conversations. A single retry recovers most cases.
            if not final_content.strip() and loop_count == 0:
                logger.warning(
                    "Target returned empty completion (no tool_calls); retrying once."
                )
                kwargs["messages"] = current_messages
                try:
                    response2 = await client.acompletion(**kwargs)
                    choices2 = _chunk_value(response2, "choices", []) or []
                    msg2 = _chunk_value(choices2[0], "message", {}) if choices2 else {}
                    content2 = _chunk_value(msg2, "content", "") or ""
                    tcs2 = _chunk_value(msg2, "tool_calls") or []
                    usage2 = _chunk_value(response2, "usage")
                    if _chunk_value(usage2, "prompt_tokens"):
                        total_input_tokens += _chunk_value(usage2, "prompt_tokens", 0)
                    if _chunk_value(usage2, "completion_tokens"):
                        total_output_tokens += _chunk_value(usage2, "completion_tokens", 0)
                    if content2.strip():
                        final_content = content2
                    elif tcs2:
                        # Second attempt produced tool_calls — fall through to
                        # the tool-execution path below by re-setting state.
                        for tc in tcs2:
                            fn = _tc_field(tc, "function", {}) or {}
                            name = _tc_field(fn, "name", "?")
                            args = _tc_field(fn, "arguments", "{}")
                            tool_call_parts.append(f"[TOOL_CALL {name}({args})]")
                            raw_tool_calls.append({"id": _tc_field(tc, "id", ""), "name": name, "arguments": args})
                        all_tool_markers.extend(tool_call_parts)
                        # Fall through to tool execution below.
                    else:
                        # Still empty after retry — accept and move on.
                        logger.warning("Target still empty after retry.")
                except Exception as exc:  # noqa: BLE001 - retry must not crash the audit
                    logger.warning("Empty-completion retry failed: %s", exc)
            if not raw_tool_calls:
                return final_content, total_input_tokens, total_output_tokens

        # Execute tool calls and append results for the next loop iteration.
        loop_count += 1
        if loop_count > max_tool_loops:
            # Budget exhausted; return what we have (tool calls serialized).
            final_content = "".join(all_tool_markers) + "".join(content_parts)
            return final_content, total_input_tokens, total_output_tokens

        # Append the assistant's tool_call message to the conversation.
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts) or None}
        assistant_msg["tool_calls"] = [
            {"id": tc["id"] or f"call_{loop_count}_{i}", "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for i, tc in enumerate(raw_tool_calls)
        ]
        current_messages.append(assistant_msg)

        # Execute each tool and append results.
        for tc in raw_tool_calls:
            result_str = _execute_tool(_tool_executor, tc["name"], tc["arguments"])
            current_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{loop_count}_{raw_tool_calls.index(tc)}",
                "content": result_str,
            })
            # Record in all_tool_markers so the final serialized content includes
            # both calls and results (the semantic judge needs to see tool outputs).
            all_tool_markers.append(f"[TOOL_RESULT {tc['name']}]{result_str}")

        # Continue the loop — the model will see the tool results.


def _build_tool_executor(tools_spec: list[dict[str, Any]]) -> Any | None:
    """Instantiate the demo Tools class if the spec matches known tool names."""
    if not tools_spec:
        return None
    names = set()
    for t in tools_spec:
        fn = t.get("function", {})
        if isinstance(fn, dict) and fn.get("name"):
            names.add(fn["name"])
    # Only instantiate the Acme demo tools if at least one matches.
    _acme_names = {"acme_get_order", "acme_get_tracking_events",
                   "acme_check_return_eligibility", "acme_get_return_case",
                   "acme_search_knowledge", "search_knowledge"}
    if names & _acme_names:
        from infra.fixtures.sample_tools.order_lookup import Tools
        return Tools()
    return None


def _execute_tool(executor: Any | None, name: str, arguments_json: str) -> str:
    """Execute a tool by name and return the result as a JSON string."""
    import json as _json

    if executor is None:
        return json.dumps({"error": f"No executor available for tool '{name}'"})
    method = getattr(executor, name, None)
    if method is None:
        return json.dumps({"error": f"Unknown tool '{name}'"})
    try:
        args = _json.loads(arguments_json) if arguments_json else {}
        if not isinstance(args, dict):
            args = {}
        result = method(**args)
        return _json.dumps(result, default=str)
    except TypeError as exc:
        return json.dumps({"error": f"Bad arguments for {name}: {exc}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - tool sandbox must not crash the loop
        return json.dumps({"error": f"Tool {name} raised: {exc}"})


class TraceContextModelTarget(ModelTarget):
    """A ``ModelTarget`` that forwards ``TargetContext.trace_headers``."""

    async def send(
        self,
        *,
        system: str | None = None,
        user: str,
        history: list[dict[str, Any]] | None = None,
        file_uri: str | list[str] | None = None,
        documents: list[str | dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        context: TargetContext | None = None,
    ) -> TargetResponse:
        request_params = dict(params or {})
        model = self._model
        if context and context.trace_headers:
            extra_headers = dict(request_params.get("extra_headers") or {})
            extra_headers.update(context.trace_headers)
            request_params["extra_headers"] = extra_headers

        # Open WebUI's OpenAI-compat endpoint only authenticates OWUI session
        # JWTs (Studio's sa_api_ keys are rejected), so when we are talking to
        # that endpoint swap the client's bearer token for a trusted-header
        # JWT. This is what lets a traced agent run reach the base model
        # through OWUI (the only path that exports OTLP spans).
        raw_client = getattr(self._client, "client", None)
        base_url = str(getattr(raw_client, "base_url", "") or "")
        if "/chat/api/v1" in base_url:
            jwt = await _owui_get_jwt(base_url)
            if jwt:
                raw_client.api_key = jwt

        # any_llm's OpenAI provider does not forward ``extra_headers`` to the
        # SDK (it silently drops it), so the W3C traceparent never reaches OWUI
        # and spans land on OWUI-generated trace ids. For the OWUI route we
        # set the headers directly on the underlying AsyncOpenAI client instead;
        # they ride along on every request of this turn. The guard for
        # ``_custom_headers`` keeps test doubles without the real SDK attribute
        # from crashing; production clients always have it.
        if context and context.trace_headers and raw_client is not None:
            custom_headers = getattr(raw_client, "_custom_headers", None)
            if isinstance(custom_headers, dict):
                custom_headers.update(context.trace_headers)

        content, input_tokens, output_tokens = await _streaming_call(
            self._client,
            model,
            system=system,
            user=user,
            response_format=response_format,
            history=history,
            file_uri=file_uri,
            documents=documents,
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
            params=request_params,
        )
        return TargetResponse(
            content=content,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
        )


def install_trace_context_target(auditor: Any) -> None:
    """Replace an auditor's default target with the context-aware adapter."""
    auditor.set_target(
        TraceContextModelTarget(
            client=auditor.target_client,
            model=auditor.target_model,
            max_retries=auditor.max_retries,
            retry_backoff=auditor.retry_backoff,
        )
    )


logger = logging.getLogger(__name__)

# Collects every TraceContextModelAuditor built while a list is installed, so
# the engine can close their async HTTP clients before the event loop that
# owns their pooled connections is shut down by ``asyncio.run()``.
_auditor_registry: contextvars.ContextVar[list[Any] | None] = contextvars.ContextVar(
    "simpleaudit_studio_auditor_registry", default=None
)

_SKIP_TYPES = (str, bytes, int, float, bool, type(None))


def _async_close_method(obj: Any):
    """Return a bound coroutine close method (``aclose`` or ``close``) or None."""
    for name in ("aclose", "close"):
        method = getattr(type(obj), name, None)
        if method is not None and inspect.iscoroutinefunction(method):
            return getattr(obj, name)
    return None


def _collect_async_clients(root: Any, *, max_depth: int = 3) -> list[Any]:
    """Find objects with an async close method reachable from ``root``'s attributes."""
    found: list[Any] = []
    seen: set[int] = {id(root)}

    def _walk(obj: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(obj, dict):
            attrs = list(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            attrs = list(obj)
        else:
            try:
                attrs = list(vars(obj).values())
            except TypeError:
                return
        for value in attrs:
            if isinstance(value, _SKIP_TYPES) or inspect.isroutine(value) or isinstance(value, type):
                continue
            if id(value) in seen:
                continue
            seen.add(id(value))
            if _async_close_method(value) is not None:
                found.append(value)
                continue
            _walk(value, depth + 1)

    _walk(root, 1)
    return found


async def aclose_auditor_clients(auditors: list[Any]) -> None:
    """Close the async HTTP clients held by ``auditors`` on the running loop.

    Must be awaited inside the same event loop the clients were used in.
    Otherwise their garbage-collection finalizer schedules ``aclose()`` on a
    later loop while the pooled transports still belong to the closed one,
    which raises ``RuntimeError: Event loop is closed``.
    """
    closed: set[int] = set()
    for auditor in auditors:
        if auditor is None:
            continue
        for client in _collect_async_clients(auditor):
            if id(client) in closed:
                continue
            closed.add(id(client))
            close = _async_close_method(client)
            if close is None:
                continue
            try:
                await close()
            except Exception:  # cleanup must never fail the scenario
                logger.debug("Failed to close async client %r", client, exc_info=True)


async def run_with_client_cleanup(coro_factory, auditors: list[Any] | None = None):
    """Await ``coro_factory()`` then close every auditor's async clients.

    Auditors constructed while the coroutine runs (e.g. the fresh per-rep
    auditors AuditExperiment builds) are captured via the registry.
    """
    registry: list[Any] = list(auditors or [])
    token = _auditor_registry.set(registry)
    try:
        return await coro_factory()
    finally:
        _auditor_registry.reset(token)
        await aclose_auditor_clients(registry)


from simpleaudit.model_auditor import ModelAuditor as _SimpleAuditModelAuditor


class TraceContextModelAuditor(_SimpleAuditModelAuditor):
    """ModelAuditor variant used by SimpleAudit's repetition runner.

    ``extra_body`` (session_id / tool_ids for Open WebUI's saved-chat path)
    arrives as a constructor kwarg from run_scenario_repeated; it is a
    per-request parameter, so pop it and merge it into target_params where
    the engine folds it into every turn's request params.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        extra_body = kwargs.pop("extra_body", None)
        super().__init__(*args, **kwargs)
        if extra_body:
            tp = dict(self.target_params or {})
            tp["extra_body"] = {**(tp.get("extra_body") or {}), **extra_body}
            self.target_params = tp
        install_trace_context_target(self)
        registry = _auditor_registry.get()
        if registry is not None:
            registry.append(self)


def decision_auditor_class(make_target: Any) -> type:
    """A ModelAuditor subclass that sends to an explicit, non-model Target.

    The repetition runner builds its own auditors, so a decision run cannot
    just call ``set_target`` on one instance — the class itself has to install
    the target. ``make_target`` is called once per auditor, since a
    ``DecisionTarget`` owns an HTTP client.

    No trace-context adapter here: that adapter wraps the OpenAI-compatible
    chat client, which a decision target does not use.
    """

    class _DecisionModelAuditor(_SimpleAuditModelAuditor):
        # Set on this class only, never on the shared ModelAuditor: see
        # infra.engine.without_target_client.
        _skip_target_client = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Popped as in TraceContextModelAuditor: ModelAuditor takes no
            # ``extra_body`` kwarg, and the model entry carries it whenever the
            # target's params do. A DecisionTarget ignores per-request params,
            # so the injected value is inert.
            extra_body = kwargs.pop("extra_body", None)
            super().__init__(*args, **kwargs)
            if extra_body:
                tp = dict(self.target_params or {})
                tp["extra_body"] = {**(tp.get("extra_body") or {}), **extra_body}
                self.target_params = tp
            self.set_target(make_target())
            # Registered like TraceContextModelAuditor, so the engine closes
            # this rep's auditor and judge clients before its loop shuts down.
            registry = _auditor_registry.get()
            if registry is not None:
                registry.append(self)

    return _DecisionModelAuditor
