"""A tiny, self-contained Open WebUI Pipelines *filter* service (no Docker).

Why this exists
---------------
Open WebUI's ``pipelines`` repo is a FastAPI server that speaks the OpenAI API
plus two extra endpoints — ``/v1/{id}/filter/inlet`` and ``/v1/{id}/filter/outlet``
— which Open WebUI calls on every chat turn to let a "filter" pipeline observe
the user request (inlet) and the assistant response (outlet). That is exactly the
seam we need to send Studio's chats to Langfuse (issue #21) without running the
full pipelines container or its torch/opencv dependency pile.

This module re-implements just that seam as one file:

    browser ─► Caddy ─► /chat/* ─► Open WebUI
                                        │  (its "openai api url")
                                        ▼
                                  :9099  ◄── this service
                                        │  inlet: build/update a Langfuse trace
                                        │  outlet: record generation + token usage
                                        └─ pipe(): forward to the real model

It is intentionally small. It only needs ``fastapi``, ``uvicorn``, ``httpx`` and
``langfuse`` (all already in the project environment), so it runs as a plain
background process with no clone and no heavy deps. The sidecar launcher lives in
``chat/pipelines_sidecar.py``; this file is the ASGI app itself.

Endpoints (mirrors open-webui/pipelines main.py):
    GET  /v1/models                 -> lists the filter as a model
    GET  /v1/pipelines              -> registered pipelines
    POST /v1/chat/completions       -> model call: capture, then forward upstream
    POST /v1/{id}/filter/inlet      -> capture user request into a trace
    POST /v1/{id}/filter/outlet     -> capture assistant reply into the trace

The bearer key defaults to the pipelines convention (``0p3n-w3bu!``); override
with SIMPLEAUDIT_PIPELINES_API_KEY. All configuration comes from the environment.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# --- Configuration (env-driven; see docs/chat.md) -------------------------
PIPELINE_ID = os.environ.get("SIMPLEAUDIT_PIPELINES_ID", "studio_capture")
API_KEY = os.environ.get("SIMPLEAUDIT_PIPELINES_API_KEY", "0p3n-w3bu!")
# Upstream OpenAI-compatible base the stub forwards model calls to. When empty,
# the stub answers with a canned completion instead of calling anything.
FORWARD_TO = os.environ.get("SIMPLEAUDIT_PIPELINES_FORWARD", "").rstrip("/")
CANNED = os.environ.get("SIMPLEAUDIT_PIPELINES_CANNED", "false").lower() == "true"

# Langfuse. Disabled unless both keys are present; then traces go to `host`.
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

app = FastAPI(title="SimpleAudit Studio Pipelines Filter", version="0.1")

_bearer = HTTPBearer(auto_error=False)


def _auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:  # noqa: B008
    # FastAPI requires Depends() in argument defaults; B008 is a false positive here.
    if creds is not None and creds.credentials != API_KEY:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")


class FilterForm(BaseModel):
    body: dict
    user: dict | None = None
    model_config = ConfigDict(extra="allow")


# --- Langfuse (lazy, optional) --------------------------------------------
_langfuse = None


def _get_langfuse():
    """Return a shared Langfuse client, or None when not configured."""
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    if not (LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY):
        return None
    try:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            secret_key=LANGFUSE_SECRET_KEY,
            public_key=LANGFUSE_PUBLIC_KEY,
            host=LANGFUSE_HOST,
        )
        logger.info("Langfuse enabled (%s)", LANGFUSE_HOST)
    except Exception:
        logger.warning("Langfuse init failed; capturing disabled", exc_info=True)
        _langfuse = None
    return _langfuse


# Stable Langfuse trace id per chat, so the inlet span and the outlet generation
# land on the same trace. A valid Langfuse trace id is 32 lowercase hex chars.
_chat_trace_ids: dict[str, str] = {}


def _chat_id(body: dict) -> str:
    metadata = body.get("metadata") or {}
    chat_id = metadata.get("chat_id") or "local"
    if chat_id == "local":
        session_id = metadata.get("session_id")
        chat_id = f"temporary-session-{session_id}" if session_id else str(uuid.uuid4())
    return chat_id


def _user_id(body: dict, user: dict | None = None) -> str | None:
    """Best-effort stable user identifier for Langfuse's Users view.

    Preference order: the filter-path ``user`` dict (email/name), then the
    OpenAI-standard ``user`` field on the request body, then a nested
    ``metadata.user``. Returns None when nothing is available so we never tag
    traces with a fabricated identity.
    """
    for candidate in (user, (body.get("metadata") or {}).get("user")):
        if isinstance(candidate, dict):
            value = candidate.get("email") or candidate.get("name") or candidate.get("id")
            if value:
                return str(value)
        elif candidate:
            return str(candidate)
    top_user = body.get("user")
    if isinstance(top_user, str) and top_user:
        return top_user
    return None


def _trace_id_for(chat_id: str) -> str:
    """A stable, valid (32-lowercase-hex) Langfuse trace id for a given chat.

    The first 32 chars of a sha256 hex digest are 32 lowercase hex chars —
    precisely the format Langfuse requires for a trace id.
    """
    trace_id = _chat_trace_ids.get(chat_id)
    if trace_id is None:
        trace_id = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:32]
        _chat_trace_ids[chat_id] = trace_id
    return trace_id


def _record_inlet(body: dict, user: dict | None) -> None:
    """Open/update the chat's trace with the user request (a span)."""
    lf = _get_langfuse()
    if lf is None:
        return
    from langfuse import propagate_attributes
    from langfuse.types import TraceContext

    chat_id = _chat_id(body)
    user_id = _user_id(body, user)
    try:
        with propagate_attributes(
            session_id=chat_id,
            user_id=user_id,
            tags=["simpleauditstudio"],
        ), lf.start_as_current_observation(
            name=f"chat:{chat_id}",
            as_type="span",
            trace_context=TraceContext(trace_id=_trace_id_for(chat_id)),
            input=body.get("messages"),
            metadata={"interface": "open-webui"},
        ):
            pass
    except Exception:
        logger.warning("Langfuse inlet failed for %s", chat_id, exc_info=True)


def _record_outlet(body: dict, user: dict | None) -> None:
    """Record the assistant reply as a generation on the chat's trace."""
    lf = _get_langfuse()
    if lf is None:
        return
    from langfuse import propagate_attributes
    from langfuse.types import TraceContext

    chat_id = _chat_id(body)
    user_id = _user_id(body, user)
    messages = body.get("messages") or []
    last_assistant = next(
        (m for m in reversed(messages) if m.get("role") == "assistant"), {}
    )
    usage = last_assistant.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or usage.get("prompt_eval_count")
    completion_tokens = usage.get("completion_tokens") or usage.get("completion_eval_count")
    usage_details = None
    if prompt_tokens is not None:
        usage_details = {"input": int(prompt_tokens), "output": int(completion_tokens or 0)}
    try:
        with propagate_attributes(
            session_id=chat_id,
            user_id=user_id,
            tags=["simpleauditstudio"],
        ), lf.start_as_current_observation(
            name="generation",
            as_type="generation",
            trace_context=TraceContext(trace_id=_trace_id_for(chat_id)),
            model=body.get("model"),
            input=messages,
            output=last_assistant.get("content"),
            usage_details=usage_details,
        ):
            pass
    except Exception:
        logger.warning("Langfuse outlet failed for %s", chat_id, exc_info=True)


# --- Routes ----------------------------------------------------------------
@app.get("/v1/models")
def list_models(_: None = Depends(_auth)):
    return {
        "object": "list",
        "data": [
            {
                "id": PIPELINE_ID,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "openai",
                "pipeline": {"type": "filter", "pipelines": ["*"], "priority": 0},
            }
        ],
        "pipelines": True,
    }


@app.get("/v1/pipelines")
def list_pipelines(_: None = Depends(_auth)):
    return {
        "data": [
            {"id": PIPELINE_ID, "name": PIPELINE_ID, "type": "filter", "valves": False}
        ]
    }


@app.post("/v1/{pipeline_id}/filter/inlet")
async def filter_inlet(pipeline_id: str, form: FilterForm, _: None = Depends(_auth)):
    if pipeline_id != PIPELINE_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Filter {pipeline_id} not found")
    _record_inlet(form.body, form.user)
    return form.body


@app.post("/v1/{pipeline_id}/filter/outlet")
async def filter_outlet(pipeline_id: str, form: FilterForm, _: None = Depends(_auth)):
    if pipeline_id != PIPELINE_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Filter {pipeline_id} not found")
    _record_outlet(form.body, form.user)
    return form.body


#: Sampling/generation params that Langfuse exposes under `model_parameters`.
# All are already present in the request body OWUI sends, so capturing them is
# free (no extra calls). Anything else in the body we keep in `metadata.raw_params`.
_MODEL_PARAM_KEYS = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
    "frequency_penalty", "presence_penalty", "seed", "stop", "n",
    "response_format", "logit_bias", "tools", "tool_choice", "parallel_tool_calls",
    "stream", "user",
)


def _model_parameters(body: dict) -> dict | None:
    """The subset of the request body Langfuse wants under `model_parameters`."""
    params = {k: body[k] for k in _MODEL_PARAM_KEYS if k in body}
    return params or None


def _record_completion(body: dict, response: dict) -> None:
    """Record a completed model call (request + response) as one trace.

    Open WebUI routes an OpenAI-connection model through /v1/chat/completions
    (the pipe path), not the filter inlet/outlet — so this is what actually
    captures a chat when the service is used as the provider's base URL.

    Captures everything that's already in the request/response at no cost:
    full messages (incl. system prompt), sampling params (model_parameters),
    tool definitions, finish reason, token usage, and raw passthrough params.
    """
    lf = _get_langfuse()
    if lf is None:
        return
    from langfuse import propagate_attributes
    from langfuse.types import TraceContext

    chat_id = _chat_id(body)
    user_id = _user_id(body)
    messages = body.get("messages") or []
    choices = response.get("choices") or [{}]
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    usage = response.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    usage_details = None
    if prompt_tokens is not None:
        usage_details = {"input": int(prompt_tokens), "output": int(completion_tokens or 0)}

    # Everything in the request body we did NOT surface elsewhere, kept verbatim
    # so nothing is lost (tools, stop sequences, provider-specific options, ...).
    known = {"messages", "model", "metadata", "user"}
    raw_params = {k: v for k, v in body.items() if k not in known}

    metadata = {
        "interface": "open-webui",
        "finish_reason": choice.get("finish_reason"),
        "requested_model": body.get("model"),
    }
    if raw_params:
        metadata["raw_params"] = raw_params
    if message.get("reasoning_content"):
        metadata["reasoning"] = message.get("reasoning_content")

    try:
        # propagate_attributes sets the first-class session_id / user_id on the
        # trace (surfaced in Langfuse's Sessions and Users views); it wraps the
        # observation so those attributes land on this generation.
        with propagate_attributes(
            session_id=chat_id,
            user_id=user_id,
            tags=["simpleauditstudio"],
        ), lf.start_as_current_observation(
            name=f"chat:{chat_id}",
            as_type="generation",
            trace_context=TraceContext(trace_id=_trace_id_for(chat_id)),
            model=response.get("model") or body.get("model"),
            model_parameters=_model_parameters(body),
            input=messages,
            output=content,
            metadata=metadata,
            usage_details=usage_details,
        ):
            pass
    except Exception:
        logger.warning("Langfuse completion record failed for %s", chat_id, exc_info=True)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _: None = Depends(_auth)):
    """Capture the model call, then forward it to the real backend (or answer canned)."""
    body = await request.json()
    if CANNED or not FORWARD_TO:
        result = _canned_completion(body)
        _record_completion(body, result)
        return result

    url = f"{FORWARD_TO}/chat/completions"
    headers = {}
    api_key = os.environ.get("SIMPLEAUDIT_PIPELINES_FORWARD_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            result = resp.json()
    except Exception as exc:  # noqa: BLE001 - surface upstream failure clearly
        logger.error("Pipelines forward to %s failed: %s", url, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Upstream error: {exc}")
    _record_completion(body, result)
    return result


def _canned_completion(body: dict) -> dict:
    last_user = ""
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            last_user = m.get("content", "")
            break
    return {
        "id": f"stub-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", PIPELINE_ID),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"[studio-capture] echo: {last_user}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def run() -> None:
    """Entry point used by the sidecar: start uvicorn on the configured port."""
    import uvicorn

    host = os.environ.get("SIMPLEAUDIT_PIPELINES_HOST", "127.0.0.1")
    port = int(os.environ.get("SIMPLEAUDIT_PIPELINES_PORT", "9099"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
