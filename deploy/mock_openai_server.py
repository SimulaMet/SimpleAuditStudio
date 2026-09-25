"""Minimal OpenAI-compatible mock for end-to-end audit execution.

This is a TEST/DEMO aid only — NOT part of the production path. It lets a fresh
``docker compose up`` prove that an audit executes Target -> Auditor -> Judge
through the live worker without requiring real model API keys.

It implements just enough of ``POST /v1/chat/completions`` for the SimpleAudit
engine's ``any_llm`` client:

- Returns ``choices[0].message.content`` + ``usage`` (prompt/completion tokens).
- Distinguishes the JUDGE call (its user prompt contains "exact JSON structure")
  and returns a valid JSON verdict object; otherwise returns a plain-text reply
  suitable for either the target or the auditor-probe role.

Run standalone:  python deploy/mock_openai_server.py 8901
Or as a compose service (see docker-compose.yml profile "mock").
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JUDGE_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_last_user_text(messages):
    """Return the last user message content as a string."""
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                return "\n".join(parts)
            return content or ""
    return ""


def _is_judge_request(messages):
    text = _extract_last_user_text(messages)
    return "exact JSON structure" in text or '"severity"' in text


def _judge_payload() -> str:
    return json.dumps(
        {
            "severity": "pass",
            "issues_found": [],
            "positive_behaviors": ["Model responded appropriately"],
            "summary": "Mock judge: conversation handled appropriately.",
            "recommendations": [],
        }
    )


def _completion_body(messages, model) -> dict:
    if _is_judge_request(messages):
        content = _judge_payload()
    else:
        # Target / auditor-probe: a short, deterministic, safe reply.
        content = (
            "I can help with that. Here is a concise, safe answer to your "
            "question based on general guidance."
        )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "mock-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, len(json.dumps(messages)) // 4),
            "completion_tokens": max(1, len(content) // 4),
            "total_tokens": max(2, (len(json.dumps(messages)) + len(content)) // 4),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Keep logs quiet but useful.
        print("[mock-openai] %s" % (fmt % args), flush=True)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/healthz"):
            self._send_json({"status": "ok"})
        elif self.path.rstrip("/") == "/v1/models":
            self._send_json(
                {
                    "object": "list",
                    "data": [{"id": "mock-model", "object": "model"}],
                }
            )
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, status=400)
            return
        if self.path.rstrip("/") == "/v1/chat/completions":
            self._send_json(_completion_body(body.get("messages"), body.get("model")))
        else:
            self._send_json({"error": "not found"}, status=404)

    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_mock_server(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the mock OpenAI server in a daemon thread.

    Args:
        port: Port to bind (0 = OS-assigned random free port).

    Returns:
        Tuple of (server_instance, actual_port).
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[mock-openai] listening on 127.0.0.1:{actual_port}", flush=True)
    return server, actual_port


def stop_mock_server(server: ThreadingHTTPServer) -> None:
    """Shut down a mock server started by start_mock_server()."""
    server.shutdown()
    server.server_close()


def main():
    port = int(os.environ.get("PORT", "8901"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[mock-openai] listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
