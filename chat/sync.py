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
    result = api.push_connections(connections_to_push() if payloads is None else payloads)
    # Studio never serves Ollama; leaving it on costs a failing request per page
    # load and an empty section in Open WebUI's settings.
    api.disable_ollama()
    # The embed is a throwaway surface: keep every chat temporary so nothing
    # piles up in Open WebUI's history.
    api.enforce_temporary_chats()
    return result


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
        raise ChatAPIError("No superuser to act as; Open WebUI's provider config needs an admin.")
    return user
