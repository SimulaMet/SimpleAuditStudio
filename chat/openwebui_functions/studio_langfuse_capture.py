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
import uuid

import httpx

# Base URL of the Studio pipelines service (no trailing slash). In the Azure
# deploy this is the studio container on the shared network; locally use
# http://127.0.0.1:9099. Leave empty to disable capturing.
STUDIO_PIPELINES_URL = os.environ.get("STUDIO_PIPELINES_URL", "http://127.0.0.1:9099").rstrip("/")
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

    async def user_message(self, message, history, user_message, chat_id=None, __user__=None, **kwargs):
        """Before the model call: report the request to the Studio stub."""
        url = self._url()
        if not url:
            return user_message
        payload = {
            "body": {
                "messages": [dict(m) for m in (history or [])] + [{"role": "user", "content": user_message}],
                "model": kwargs.get("model") or {},
                "metadata": {"chat_id": chat_id or str(uuid.uuid4())},
            },
            "user": self._user_payload(__user__),
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{url}/v1/{PIPELINE_ID}/filter/inlet", json=payload, headers=self._headers())
        except Exception:
            pass  # tracing must never break chat
        return user_message

    async def after_chat_completion(self, content, context=None, __user__=None, **kwargs):
        """After the model reply: report the assistant message to the Studio stub."""
        url = self._url()
        if not url:
            return content
        ctx = context or {}
        messages = list(ctx.get("messages") or [])
        if isinstance(content, str):
            messages.append({"role": "assistant", "content": content})
        elif isinstance(content, dict):
            messages.append(content)
        payload = {
            "body": {
                "chat_id": ctx.get("chat_id") or "local",
                "messages": messages,
                "metadata": ctx.get("metadata") or {},
            },
            "user": self._user_payload(__user__),
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{url}/v1/{PIPELINE_ID}/filter/outlet", json=payload, headers=self._headers())
        except Exception:
            pass
        return content
