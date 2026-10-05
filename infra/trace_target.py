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

        from simpleaudit.model_auditor import ModelAuditor

        content, input_tokens, output_tokens = await ModelAuditor._call_async(
            self._client,
            self._model,
            system,
            user,
            response_format=response_format,
            history=history,
            file_uri=file_uri,
            documents=documents,
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
            params=request_params or None,
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
