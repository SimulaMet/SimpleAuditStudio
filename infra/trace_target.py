"""SimpleAudit target adapter that preserves per-turn trace context.

SimpleAudit 0.3.1 exposes ``TargetContext.trace_headers`` but its default
``ModelTarget`` does not forward that context to the OpenAI-compatible client.
This adapter keeps the normal model target behavior while adding the W3C
headers to the target request.
"""
from __future__ import annotations

from typing import Any

from simpleaudit.targets.base import TargetContext, TargetResponse
from simpleaudit.targets.model import ModelTarget


def _chunk_value(value: Any, key: str, default: Any = None) -> Any:
    """Read an OpenAI-compatible stream field from an object or mapping."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


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
