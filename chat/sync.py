"""Keeping Open WebUI's providers in step with Studio's model connections.

``push_now`` does the work; ``schedule_push`` is what the signals in
``chat/signals.py`` call. Pushing is deliberately kept off the request's path:

  * it runs after the transaction commits, so Open WebUI never sees a row that
    was rolled back,
  * in a background thread, so saving a connection does not wait on a second
    service,
  * debounced, so editing three connections (or a bulk import) is one push,
  * and best-effort: a chat that is down, still starting, or disabled is logged
    and forgotten. Studio's own data is the source of truth, and the next push —
    or ``manage.py sync_chat_models`` — catches up.
"""
from __future__ import annotations

import logging
import os
import threading

from chat import config
from chat.api import ChatAPI, ChatAPIError, connection_payload

logger = logging.getLogger(__name__)


class NoAdminError(ChatAPIError):
    """No Studio superuser exists to act as the Open WebUI admin.

    Unlike a transient ``ChatAPIError`` (Open WebUI down or still starting),
    this is a misconfiguration: every push will keep failing until a
    superuser is created, so callers should log it loudly.
    """


#: How long to wait for more changes before pushing. A save is rarely alone:
#: the connections page writes a connection and its models in one go.
DELAY_SECONDS = float(os.environ.get("SIMPLEAUDIT_CHAT_SYNC_DELAY", "2"))

_timer: threading.Timer | None = None
_timer_lock = threading.Lock()


def connections_to_push() -> list[dict]:
    """Every enabled connection that has somewhere to point at."""
    from model_registry.models import ModelConnection

    payloads = (connection_payload(conn) for conn in ModelConnection.objects.filter(enabled=True))
    return [payload for payload in payloads if payload["base_url"]]


def push_now(payloads: list[dict] | None = None) -> dict[str, int]:
    """Push connections (all of them by default). Raises ChatAPIError on refusal."""
    api = ChatAPI.as_user(_admin())
    pushed = connections_to_push() if payloads is None else payloads
    result = api.push_connections(pushed)
    # Studio never serves Ollama; leaving it on costs a failing request per page
    # load and an empty section in Open WebUI's settings.
    api.disable_ollama()
    _reconcile_model_ids(api, pushed)
    from chat.access_sync import reconcile_safely

    reconcile_safely()
    return result


def _reconcile_model_ids(api: ChatAPI, pushed: list[dict]) -> None:
    """Warn when a pinned Studio model id is not one Open WebUI registers.

    The ``?models=`` pin only takes effect when the id exactly matches a model
    Open WebUI knows; otherwise the chat silently falls back to its default
    model. OpenAI-compatible providers register models by the id their
    ``/v1/models`` endpoint returns, which can differ from Studio's
    ``model_id`` — so a mismatch is easy to create and invisible at chat time.
    Never raises: reconciliation is a diagnostic, not part of the push.
    """
    wanted = [
        (payload["name"], payload["model_ids"])
        for payload in pushed
        if payload.get("model_ids")
    ]
    if not wanted:
        return
    try:
        registered = set(api.list_models())
    except ChatAPIError as exc:
        logger.info("Chat model id reconciliation skipped: %s", exc)
        return
    for name, model_ids in wanted:
        missing = [model_id for model_id in model_ids if model_id not in registered]
        if missing:
            logger.warning(
                "Chat model pin will not work for connection '%s': Studio model "
                "id(s) %s are not registered in Open WebUI (the provider "
                "returned different ids), so the chat will fall back to the "
                "default model.",
                name, ", ".join(missing),
            )


def schedule_push(reason: str = "") -> None:
    """Push soon, once, in the background. Never raises."""
    if not config.ENABLED:
        return
    global _timer
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DELAY_SECONDS, _run, args=(reason,))
        _timer.daemon = True
        _timer.start()


def _run(reason: str) -> None:
    global _timer
    with _timer_lock:
        _timer = None
    try:
        result = push_now()
    except NoAdminError:
        # A real misconfiguration, not a transient state: every push will keep
        # failing until someone creates a superuser, so say so loudly.
        logger.warning(
            "Chat model sync failed (%s): no Studio superuser exists to act as "
            "the Open WebUI admin — create a superuser, then run "
            "`manage.py sync_chat_models`",
            reason or "change",
        )
    except ChatAPIError as exc:
        # Expected while Open WebUI is still starting, or when it is not running
        # at all. Not worth a traceback.
        logger.info("Chat model sync skipped (%s): %s", reason or "change", exc)
    except Exception:
        logger.warning("Chat model sync failed (%s)", reason or "change", exc_info=True)
    else:
        logger.info(
            "Chat models synced (%s): pushed %d, kept %d",
            reason or "change", result["pushed"], result["kept"],
        )


def _admin():
    """A Studio user Open WebUI treats as an admin — provider config needs one."""
    from accounts.models import User

    user = User.objects.filter(is_superuser=True).order_by("id").first()
    if user is None:
        raise NoAdminError("No superuser to act as; Open WebUI's provider config needs an admin.")
    return user
