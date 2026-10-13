"""
title: Studio Langfuse Capture
author: SimpleAudit Studio
version: 1.0.0
description: >-
  Pure tap for SimpleAudit Studio chat tracing. Forwards every chat turn to the
  Studio pipelines service (inlet before the model call, outlet after the reply),
  which records it in Langfuse. Works with ALL connections — no base URL changes,
  no provider knowledge. Tracing failures never break chat.
requirements: httpx>=0.27
"""

import os
import socket
import time
import uuid

import httpx


# Base URL of the Studio pipelines service (no trailing slash). In dev /
# single-container setups Open WebUI and Studio share one host, so loopback
# works; in the Azure multi-container deploy set STUDIO_PIPELINES_URL to the
# studio container name (e.g. http://helsedir-studio:9099). Leave empty to
# disable capturing.
def _default_pipelines_url() -> str:
    if os.environ.get("STUDIO_PIPELINES_URL"):
        return os.environ["STUDIO_PIPELINES_URL"].rstrip("/")
    try:
        socket.create_connection(("helsedir-studio", 9099), timeout=0.2)
        return "http://helsedir-studio:9099"
    except OSError:
        return "http://127.0.0.1:9099"


STUDIO_PIPELINES_URL = _default_pipelines_url()
PIPELINE_ID = os.environ.get("STUDIO_PIPELINES_ID", "studio_capture")
# Optional bearer key; the Studio stub accepts a missing or matching key.
STUDIO_PIPELINES_KEY = os.environ.get("STUDIO_PIPELINES_API_KEY", "")


class Filter:
    class Valves:
        # If set in the admin UI, overrides STUDIO_PIPELINES_URL at runtime.
        pipelines_url: str = ""

    def __init__(self):
        self.valves = self.Valves()

    def _url(self) -> str:
        return (self.valves.pipelines_url or STUDIO_PIPELINES_URL).rstrip("/")

    def _headers(self) -> dict:
        headers = {}
        if STUDIO_PIPELINES_KEY:
            headers["Authorization"] = f"Bearer {STUDIO_PIPELINES_KEY}"
        return headers

    @staticmethod
    def _user_payload(__user__) -> dict | None:
        if not __user__:
            return None
        return {
            "id": __user__.get("id"),
            "name": __user__.get("name"),
            "email": __user__.get("email"),
        }

    async def inlet(self, body, __model__=None, __chat_id__=None, __user__=None, **kwargs):
        """Before the model call: report the full request to the Studio stub."""
        url = self._url()
        if not url:
            return body
        messages = body.get("messages") or []
        cid = __chat_id__ or body.get("chat_id") or str(uuid.uuid4())
        model_info = __model__ or {}
        # Capture everything available: params, features, metadata.
        params = body.get("params") or {}
        payload = {
            "body": {
                "chat_id": cid,
                "messages": [dict(m) for m in messages],
                "model": model_info.get("id", ""),
                "model_name": model_info.get("name", ""),
                "params": params,
                "features": body.get("features") or {},
                "stream": body.get("stream", False),
                "timestamp": time.time(),
            },
            "user": self._user_payload(__user__),
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{url}/v1/{PIPELINE_ID}/filter/inlet", json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001 - tracing must never break chat
            print(f"[studio-capture] inlet failed: {exc!r}")
        return body

    async def outlet(self, body, __model__=None, __chat_id__=None, __user__=None, **kwargs):
        """After the model reply: report full response data to the Studio stub.

        ``body`` is a dict with keys: model, messages, chat_id, session_id, id.
        Each message has role, content, info, timestamp, usage (on assistant).
        """
        url = self._url()
        if not url:
            return body
        cid = body.get("chat_id") or __chat_id__ or "local"
        messages = body.get("messages") or []
        # Keep all fields from each message — content, usage, info, timestamp.
        slim_messages = [
            {
                "role": m.get("role"),
                "content": m.get("content", ""),
                **({"usage": m["usage"]} if m.get("usage") else {}),
                **({"info": m["info"]} if m.get("info") else {}),
                **({"timestamp": m["timestamp"]} if m.get("timestamp") is not None else {}),
            }
            for m in messages
        ]
        model_info = __model__ or {}
        model_id = model_info.get("id") if isinstance(model_info, dict) else None
        payload = {
            "body": {
                "chat_id": cid,
                "messages": slim_messages,
                "model": model_id or body.get("model") or "",
                "model_name": model_info.get("name", "") if isinstance(model_info, dict) else "",
                "session_id": body.get("session_id"),
                "message_id": body.get("id"),
                "timestamp": time.time(),
            },
            "user": self._user_payload(__user__),
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{url}/v1/{PIPELINE_ID}/filter/outlet", json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001 - tracing must never break chat
            print(f"[studio-capture] outlet failed: {exc!r}")
        return body
