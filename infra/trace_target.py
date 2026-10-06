"""SimpleAudit target adapter that preserves per-turn trace context.

SimpleAudit 0.3.1 exposes ``TargetContext.trace_headers`` but its default
``ModelTarget`` does not forward that context to the OpenAI-compatible client.
This adapter keeps the normal model target behavior while adding the W3C
headers to the target request.
"""
from __future__ import annotations

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


_OPENWEBUI_AGENT_UNSUPPORTED_PARAMS = frozenset({
    "reasoning_effort",
    "search_mode",
    "top_k",
    "relevance_threshold",
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
    if raw_client is None or "/chat/api/v1" not in base_url or not extra_body.get("session_id"):
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

    for _ in range(360):
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
    raise TimeoutError("Open WebUI saved chat did not complete within 180 seconds")


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

    saved_chat_result = await _openwebui_saved_chat_call(
        client,
        model,
        kwargs=kwargs,
        trace_headers=kwargs.get("extra_headers"),
    )
    if saved_chat_result is not None:
        return saved_chat_result

    attempt = 0
    while True:
        try:
            response = await client.acompletion(**kwargs)
            break
        except Exception:
            if attempt >= max_retries:
                raise
            import asyncio

            await asyncio.sleep(retry_backoff * (2**attempt))
            attempt += 1

    content_parts: list[str] = []
    input_tokens = output_tokens = 0
    if not hasattr(response, "__aiter__"):
        choices = _chunk_value(response, "choices", []) or []
        message = _chunk_value(choices[0], "message", {}) if choices else {}
        content = _chunk_value(message, "content", "") or ""
        usage = _chunk_value(response, "usage")
        return (
            content,
            _chunk_value(usage, "prompt_tokens", 0) or 0,
            _chunk_value(usage, "completion_tokens", 0) or 0,
        )
    async for chunk in response:
        choices = _chunk_value(chunk, "choices", []) or []
        if choices:
            delta = _chunk_value(choices[0], "delta", {}) or {}
            content = _chunk_value(delta, "content", "") or ""
            if content:
                content_parts.append(content)
        usage = _chunk_value(chunk, "usage")
        if usage:
            input_tokens = _chunk_value(usage, "prompt_tokens", 0) or 0
            output_tokens = _chunk_value(usage, "completion_tokens", 0) or 0
    return "".join(content_parts), input_tokens, output_tokens


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
        if context and context.trace_headers:
            extra_headers = dict(request_params.get("extra_headers") or {})
            extra_headers.update(context.trace_headers)
            request_params["extra_headers"] = extra_headers

        content, input_tokens, output_tokens = await _streaming_call(
            self._client,
            self._model,
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


from simpleaudit.model_auditor import ModelAuditor as _SimpleAuditModelAuditor


class TraceContextModelAuditor(_SimpleAuditModelAuditor):
    """ModelAuditor variant used by SimpleAudit's repetition runner."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        install_trace_context_target(self)
