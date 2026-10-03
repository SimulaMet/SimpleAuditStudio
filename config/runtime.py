"""How Studio is currently being run: one place that answers "which mode".

A *mode* is the coarse choice of how the whole stack runs — process model,
database, queue, and whether chat ships. Before this module that choice was
scattered across three env flags (``SIMPLEAUDIT_MINIMAL``,
``SIMPLEAUDIT_LOCAL_SQLITE``, ``SIMPLEAUDIT_CHAT``) plus per-entry-point
hard-coding, so two "the same" commands behaved differently.

``resolve_mode()`` is a **lens** over those flags: it does not replace them, it
reads the same signals the settings/entry points already read and reports them
as one small struct. Keeping it additive is deliberate — nothing that branches
on the raw flags changes behavior.

Import this module *before* Django loads settings (``manage.py`` does) so the
mode is known early enough to drive ``SIMPLEAUDIT_LOCAL_SQLITE``.

The four supported modes
-------------------------
=============  =============================  =====  =======================
mode id        command                         db     queue
=============  =============================  =====  =======================
single-docker  ``docker run <image>``          sqlite embedded (in container)
compose        ``docker compose up -d``        postgres hatchet container
embedded       ``uvx simpleaudit-studio``      sqlite embedded
dev            ``manage.py dev`` (or           sqlite embedded
               ``dev_server --embedded``)
=============  =============================  =====  =======================
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: The four supported modes, in order. Anything else is a user error.
VALID_MODES = ("single-docker", "compose", "embedded", "dev")


class UnknownModeError(ValueError):
    """Raised when ``SIMPLEAUDIT_MODE`` is set to a value we do not know."""


@dataclass(frozen=True)
class ModeProfile:
    """A resolved, read-only description of the current run mode."""

    id: str            # one of VALID_MODES
    process: str       # "single" | "multi-container"
    database: str      # "sqlite" | "postgres"
    queue: str         # "embedded" | "external"
    chat: str          # "on" | "opt-in" (per-mode default; the effective value
                       # still honors SIMPLEAUDIT_CHAT / --disable-chat)
    debug: bool

    def as_dict(self) -> dict:
        return {
            "mode": self.id,
            "process": self.process,
            "database": self.database,
            "queue": self.queue,
            "chat": self.chat,
            "debug": self.debug,
        }


_PROFILES: dict[str, ModeProfile] = {
    "single-docker": ModeProfile(
        id="single-docker", process="single",
        database="sqlite", queue="embedded", chat="on", debug=False,
    ),
    "compose": ModeProfile(
        id="compose", process="multi-container",
        database="postgres", queue="external", chat="opt-in", debug=False,
    ),
    "embedded": ModeProfile(
        id="embedded", process="single",
        database="sqlite", queue="embedded", chat="on", debug=False,
    ),
    "dev": ModeProfile(
        id="dev", process="single",
        database="sqlite", queue="embedded", chat="on", debug=True,
    ),
}


def profile_for(mode_id: str) -> ModeProfile:
    if mode_id not in _PROFILES:
        raise UnknownModeError(
            f"Unknown SIMPLEAUDIT_MODE {mode_id!r}. Valid: {', '.join(VALID_MODES)}"
        )
    return _PROFILES[mode_id]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _compose_active() -> bool:
    """Heuristic: is this a Compose service? Compose runs Postgres + an external
    Hatchet. It is only inferred when we are clearly *not* in a SQLite /
    single-process setup (which the ``.env.local.example`` otherwise mimics,
    since it carries POSTGRES_* and HATCHET_* values for later switching)."""
    if _truthy(os.environ.get("SIMPLEAUDIT_LOCAL_SQLITE")):
        return False
    if _truthy(os.environ.get("SIMPLEAUDIT_MINIMAL")):
        return False
    pg_host = (os.environ.get("POSTGRES_HOST") or "").strip()
    pg_port = (os.environ.get("POSTGRES_PORT") or "").strip()
    has_hatchet = bool((os.environ.get("HATCHET_SERVER_URL") or "").strip())
    return bool(pg_host and pg_port and has_hatchet)


def resolve_mode() -> ModeProfile:
    """Return the resolved :class:`ModeProfile`.

    An explicit ``SIMPLEAUDIT_MODE`` wins and is validated. Otherwise the mode
    is inferred from the signals the entry points already set:
    ``SIMPLEAUDIT_MINIMAL`` → ``embedded`` (single-process demo), a Compose
    marker → ``compose``. If nothing resolves, that is a configuration error —
    the legacy "just connect to whatever is in ``.env``" path is gone.
    """
    explicit = (os.environ.get("SIMPLEAUDIT_MODE") or "").strip().lower()
    if explicit:
        return profile_for(explicit)

    if _truthy(os.environ.get("SIMPLEAUDIT_MINIMAL")):
        return _PROFILES["embedded"]

    if _compose_active():
        return _PROFILES["compose"]

    raise UnknownModeError(
        "Could not determine the run mode. Set SIMPLEAUDIT_MODE to one of: "
        + ", ".join(VALID_MODES)
        + "."
    )


def effective_chat_mode(mode_id: str, disable_flag: bool = False) -> str:
    """The ``SIMPLEAUDIT_CHAT`` value an entry point should run with.

    Precedence (highest first): the ``--disable-chat`` flag, then an explicit
    ``SIMPLEAUDIT_CHAT`` env value, then the mode default (on for the
    single-process modes, off for compose). The flag wins over the environment
    on purpose: a disable flag is an explicit, in-the-moment opt-out, so chat
    stays *always* disableable even if a ``.env`` pins it on.

    Returns ``"off"`` or a chat mode (``"embedded"`` / ``"docker"``). The chat
    module still does the final interpretation of the value.
    """
    if disable_flag:
        return "off"
    env = (os.environ.get("SIMPLEAUDIT_CHAT") or "").strip().lower()
    if env:  # explicit env wins over the mode default
        return env
    if profile_for(mode_id).chat != "on":
        return "off"
    return "embedded"
