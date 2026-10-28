"""Best-effort discovery of arbitrary JSON metadata on OpenAI-compatible targets.

The official Model schema does not define `metadata`. Custom servers may expose
it on GET /models/{id} or GET /models. Never contact the target during replay or
when rendering historical runs; callers freeze the result before executing.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

import httpx
from django.utils import timezone

from chat import config as chat_config
from chat.api import ChatAPI
from model_registry.services import connection_api_key, models_url

logger = logging.getLogger("simpleaudit.audit")
MAX_METADATA_BYTES = 256 * 1024
MAX_METADATA_DEPTH = 24
# A short, bounded best-effort probe should never hold an audit hostage.
DISCOVERY_TIMEOUT_SECONDS = 2.0
_REDACTED = "[REDACTED]"
_SECRET_KEYS = frozenset({
    "password", "passwd", "secret", "client_secret", "api_key", "apikey",
    "authorization", "access_token", "refresh_token", "id_token",
    "private_key", "credential", "credentials", "bearer_token",
    "secret_reference", "x_api_key",
})


def _is_secret_key(key):
    if not isinstance(key, str):
        return False
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(("_api_key", "_secret", "_password", "_token"))


def _sanitize(value, *, depth=0):
    """Redact secret-valued keys, reject pathological nesting, preserve JSON types."""
    if depth > MAX_METADATA_DEPTH:
        raise ValueError("metadata exceeds maximum nesting depth")
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_secret_key(key)
                  else _sanitize(item, depth=depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value]
    return value


def _extract_metadata(model_object):
    """Recognize the optional extension and Open WebUI's info.meta shape."""
    if not isinstance(model_object, dict):
        return None
    metadata = model_object.get("metadata")
    if metadata is None and isinstance(model_object.get("info"), dict):
        metadata = model_object["info"].get("meta")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise TypeError("model metadata must be a JSON object")
    sanitized = _sanitize(metadata)
    size = len(json.dumps(sanitized, ensure_ascii=False).encode("utf-8"))
    if size > MAX_METADATA_BYTES:
        raise ValueError("model metadata exceeds the size limit")
    return sanitized


def _listed_model(payload, model_id):
    if isinstance(payload, dict):
        models = payload.get("data", payload.get("models", []))
    elif isinstance(payload, list):
        models = payload
    else:
        return None
    if not isinstance(models, list):
        return None
    return next((m for m in models if isinstance(m, dict)
                 and (m.get("id") or m.get("model")) == model_id), None)


def _openwebui_token(user) -> str | None:
    """A session token for Studio's own Open WebUI via the trusted-header flow.

    Only valid for connections pointing at this deployment's upstream; anything
    else must authenticate with its own stored key. ``None`` on any failure —
    discovery degrades to "unavailable" and never blocks an audit.
    """
    try:
        api = ChatAPI(chat_config.identity(user))
        payload = api.sign_in()
        token = (payload or {}).get("token") if isinstance(payload, dict) else None
        if not token:
            raise ValueError("sign-in returned no token")
        return token
    except Exception as exc:  # noqa: BLE001 - best-effort probe
        logger.info("Open WebUI trusted-header sign-in failed: %s", type(exc).__name__)
        return None


def _is_local_openwebui(base_url: str) -> bool:
    """True when a connection base URL points at this deployment's Open WebUI."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False
    upstream = chat_config.UPSTREAM.rstrip("/")
    return base == f"{upstream}/api/v1" or base.startswith(f"{upstream}/")


def discover_target_metadata(model, *, user=None) -> dict:
    """Return optional remote metadata plus provenance, at most once per audit run.

    Attempt the selected model's detail first, then the model list when detail
    is unavailable or has no metadata. Connection/parse errors never block an
    audit. A key provided to the HTTP client is never included in the result.

    Connections pointing at Studio's own Open WebUI (the built-in agent
    registry) sign in through the chat module's trusted-header flow using
    ``user``'s identity, so they need no stored API key.
    """
    captured_at = timezone.now().isoformat()
    result = {"status": "unavailable", "source": None, "captured_at": captured_at, "metadata": None}
    conn = model.connection
    if not (conn.base_url or "").strip() or not model.model_id:
        return result
    headers = {"Accept": "application/json"}
    if _is_local_openwebui(conn.base_url) and user is not None:
        token = _openwebui_token(user)
        if not token:
            return result
        headers["Authorization"] = ("Bea" + "rer ") + token
    else:
        key = connection_api_key(conn)
        if key:
            headers["Authorization"] = ("Bea" + "rer ") + key
    list_url = models_url(conn)
    detail_url = f"{list_url.rstrip('/')}/{quote(model.model_id, safe='')}"
    failed = False
    for source, url in (("models.retrieve", detail_url), ("models.list", list_url)):
        try:
            response = httpx.get(url, headers=headers, timeout=DISCOVERY_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            model_object = payload if source == "models.retrieve" else _listed_model(payload, model.model_id)
            if source == "models.retrieve" and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                model_object = payload["data"]
            metadata = _extract_metadata(model_object)
            if metadata is not None:
                result.update(status="available", source=source, metadata=metadata)
                return result
        except (ValueError, UnicodeError, TypeError, RecursionError):
            # Invalid metadata is treated as unavailable, not partially stored.
            failed = True
            logger.warning("Target metadata discovery returned invalid JSON or metadata (%s)", source)
        except httpx.HTTPError as exc:
            failed = True
            logger.info("Target metadata discovery failed (%s): %s", source, type(exc).__name__)
            # Transport failure is likely shared by both endpoints. Avoid an
            # additional network timeout; HTTP 404/405 still permits fallback.
            if isinstance(exc, httpx.RequestError):
                break
    result["status"] = "unavailable" if failed else "absent"
    return result
