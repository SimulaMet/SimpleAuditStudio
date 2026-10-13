"""Studio's OpenAI-compatible model API, keyed by a personal API token.

External clients call ``GET /api/v1/models`` and ``POST /api/v1/chat/completions``
with ``Authorization: ****** The key acts as its owner: requests are scoped to
the models the owner can see in their workspace, and completions are proxied to
Open WebUI running as that user (so the owner's provider config, RAG, and tools
all apply). This is what issue #24 asks for — a stable endpoint + token other
applications can integrate against.

Why Studio builds this instead of exposing Open WebUI's own ``/chat/api/v1``:
that surface sits behind Caddy's forward-auth, which bounces any request without
a Studio *session cookie* to ``/login/`` — an external client has none, so its
Bearer key would never reach OWUI. These endpoints live under Studio's own
routing (no forward-auth gate) and authenticate with a key we own.

The two kinds of endpoints mirror ``model_registry/otlp_views.py``:

1. **Machine-to-machine** — ``/api/v1/models`` and ``/api/v1/chat/completions``,
   authenticated by the ``Authorization`` header, not a Studio session.
2. **Credential management** — session-authenticated DRF views that issue/revoke
   the caller's own key (the plaintext token is returned exactly once).
"""
from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from chat import config as chat_config
from infra.exceptions import StableAPIError
from model_registry import api_credential_services as api_cred

logger = logging.getLogger(__name__)


# --- shared helpers ----------------------------------------------------------

def _bearer_token(request) -> str | None:
    """Extract the bearer token from the Authorization header.

    Reads ``META['HTTP_AUTHORIZATION']`` (the canonical Django location, always
    populated by WSGI) rather than the DRF ``Request.headers`` proxy, which is
    not reliably available at this point in the view.
    """
    auth = request.META.get("HTTP_AUTHORIZATION", "") or ""
    parts = auth.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _visible_agent_ids(project) -> list[str]:
    """Synced agent ids (``studio.agent-<pk>``) visible in this workspace."""
    from model_registry.models import Agent

    return sorted(
        Agent.objects.filter(project=project, enabled=True)
        .exclude(external_id="")
        .values_list("external_id", flat=True)
        .distinct()
    )


def _visible_model_ids(user, project) -> list[str]:
    """Every externally-visible model id the user can use in this workspace.

    Two kinds, both callable through the same OpenAI-compatible surface:
      * bare models — ``<connection id>.<model id>``, registered once Studio
        pushes a connection (see ``chat.api.chat_model_prefix``);
      * synced agents — their Open WebUI workspace-model id
        (``studio.agent-<pk>``), which runs the agent's full pipeline.
    """
    from model_registry.services import visible_connections_for

    ids: list[str] = []
    for conn in visible_connections_for(user, project):
        if not conn.enabled or not (conn.base_url or "").strip():
            continue
        prefix = f"{conn.id}."
        for model_id in conn.models.filter(enabled=True).values_list("model_id", flat=True).distinct():
            ids.append(f"{prefix}{model_id}")
    ids.extend(_visible_agent_ids(project))
    return sorted(ids)


#: Prefix of an agent's Open WebUI workspace-model id (``studio.agent-<pk>``).
AGENT_ID_PREFIX = "studio.agent-"


def _is_agent_id(model_field: str) -> bool:
    return (model_field or "").startswith(AGENT_ID_PREFIX)


def _resolve_target(user, project, model_field: str):
    """Resolve a requested model id to a visible, enabled target.

    Accepts either a bare model (``<conn>.<model_id>``) or a synced agent
    (``studio.agent-<pk>``). Raises 404 when the id is malformed, the target
    doesn't exist, or the user cannot see it — so a hand-typed id for a hidden
    resource is rejected even if the bare id exists elsewhere.
    """
    from model_registry.models import Agent
    from model_registry.services import visible_connections_for

    # Agents: deterministic ``studio.agent-<pk>`` id, scoped to the workspace.
    if _is_agent_id(model_field):
        agent = Agent.objects.filter(project=project, enabled=True, external_id=model_field).first()
        if agent is None:
            raise _ApiAuthError("Unknown model.", "unknown_model", 404)
        return agent, None, model_field

    # Bare models: ``<connection id>.<model id>``.
    if "." not in model_field:
        raise _ApiAuthError("Model id must be '<connection id>.<model id>' or an agent id.", "invalid_model_id", 404)
    conn_part, _, bare_id = model_field.partition(".")
    if not conn_part.isdigit():
        raise _ApiAuthError("Unknown model.", "unknown_model", 404)

    conn = next(
        (c for c in visible_connections_for(user, project) if c.id == int(conn_part)),
        None,
    )
    if conn is None or not conn.enabled or not (conn.base_url or "").strip():
        raise _ApiAuthError("Unknown model.", "unknown_model", 404)
    if not conn.models.filter(enabled=True, model_id=bare_id).exists():
        raise _ApiAuthError("Unknown model.", "unknown_model", 404)
    return conn, bare_id, model_field


class _ApiAuthError(Exception):
    """A machine-to-machine request that must be rejected before any work."""

    def __init__(self, message: str, code: str, status: int):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _error(message: str, code: str, status: int) -> JsonResponse:
    # Mirror infra.exceptions._stable_payload so clients see one consistent
    # error shape across Studio's APIs.
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _require_chat_or_raise() -> None:
    if not chat_config.ENABLED:
        raise _ApiAuthError("The chat module is disabled on this instance.", "chat_disabled", 404)


def _resolve_credential_or_raise(request):
    """Authenticate a machine-to-machine request via its bearer token.

    Returns ``(user, project)`` or raises :class:`_ApiAuthError`. The credential's
    owning project scopes model visibility.
    """
    token = _bearer_token(request)
    cred = api_cred.verify_bearer(token) if token else None
    if cred is None:
        raise _ApiAuthError("Invalid or revoked API key.", "unauthorized", 401)
    return cred.user, cred.project


def _guard(view):
    """Wrap a machine-to-machine view: catch auth/validation errors + log upstream."""
    from functools import wraps

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _ApiAuthError as exc:
            return _error(exc.message, exc.code, exc.status)
        except Exception:
            logger.exception("api/v1 %s failed", request.path)
            return _error("Internal error while handling the request.", "internal", 500)

    return wrapper


# --- machine-to-machine: the OpenAI-compatible surface -----------------------
# Plain Django views (not DRF): external clients authenticate with a bearer key
# we verify ourselves, so DRF's session/token auth would only get in the way.
# csrf_exempt because these are machine-to-machine (no CSRF token available).

@csrf_exempt
@_guard
def api_models(request):
    """List the models this API key can call (OpenAI ``/v1/models`` shape)."""
    _require_chat_or_raise()
    user, project = _resolve_credential_or_raise(request)
    data = [{"id": mid, "object": "model"} for mid in _visible_model_ids(user, project)]
    return JsonResponse({"object": "list", "data": data})


@csrf_exempt
@_guard
def api_chat_completions(request):
    """Run a completion through Open WebUI as the key's owner.

    Accepts the standard OpenAI chat-completions body; ``model`` must be one of
    the ids from ``/api/v1/models``. Non-streaming only (streaming is a follow-up).
    """
    _require_chat_or_raise()
    user, project = _resolve_credential_or_raise(request)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise _ApiAuthError("Request body must be valid JSON.", "bad_json", 400) from exc
    if not isinstance(body, dict):
        raise _ApiAuthError("Request body must be a JSON object.", "bad_body", 400)

    model_field = body.get("model")
    messages = body.get("messages")
    if not model_field or not isinstance(messages, list):
        raise _ApiAuthError(
            "'model' (a '<connection id>.<model id>' string) and 'messages' (a list) are required.",
            "bad_body", 400,
        )

    _conn, _bare_id, prefixed_id = _resolve_target(user, project, model_field)

    # Build the payload OWUI expects: the prefixed model id plus whatever the
    # client sent (temperature, max_tokens, top_p, ...), forcing non-streaming.
    payload = {k: v for k, v in body.items() if k not in {"model"}}
    payload["model"] = prefixed_id
    payload["stream"] = False

    from chat.api import ChatAPI, ChatAPIError

    try:
        response = ChatAPI.as_user(user).chat_completion(payload)
    except ChatAPIError as exc:
        raise _ApiAuthError(f"Upstream model call failed: {exc}", "upstream_error", 502) from exc
    return JsonResponse(response)


# --- credential management: issue / revoke the caller's own key --------------

def _active_project(request):
    project = getattr(request, "project", None)
    if project is None:
        raise StableAPIError(detail="No active workspace.", code="no_workspace", http_status=400)
    return project


def _endpoint_base(request, origin: str | None = None) -> str:
    """The absolute base URL a client points its OpenAI SDK at (ends in /api/v1).

    Prefers the browser's own ``origin`` (sent by the UI) so the URL reflects
    what the user actually sees — the real domain, not a reverse-proxy artifact.
    """
    origin = (origin or "").strip()
    if origin.startswith(("http://", "https://")):
        return f"{origin.rstrip('/')}/api/v1"
    host = request.headers.get("Host") or request.get_host()
    scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{host}/api/v1"


def _require_chat_enabled_drf() -> None:
    if not chat_config.ENABLED:
        raise StableAPIError(
            detail="The chat module is disabled on this instance.",
            code="chat_disabled", http_status=404,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def issue_api_key(request):
    """Issue (or rotate) the caller's personal API key. Returns the token once."""
    _require_chat_enabled_drf()
    project = _active_project(request)
    new_cred = api_cred.upsert_credential(project=project, user=request.user)
    return Response({
        "token": new_cred.secret,
        "endpoint": _endpoint_base(request, origin=(request.data.get("origin") or "").strip()),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def revoke_api_key(request):
    """Revoke the caller's personal API key so it no longer works."""
    _require_chat_enabled_drf()
    revoked = api_cred.revoke_credential(request.user)
    return Response({"ok": True, "revoked": revoked})
