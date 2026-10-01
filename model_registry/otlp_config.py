"""Whether Studio's OTLP listener is switched on.

The OTLP listener (``POST /otlp/v1/traces``) plus its credential-management
API and UI are a self-contained Studio feature: they let an external target
push OpenTelemetry spans in and let an admin issue the credentials that gate
them. They have no bearing on the audit engine itself, so a deployment that
doesn't want them can switch the whole thing off.

**On by default** to preserve the existing behavior: the listener has always
been wired unconditionally, and existing chat-OTLP setups depend on it. Set
``SIMPLEAUDIT_OTLP`` to any of the "off" spellings below to 404 the
``/otlp/*`` routes and the credential UI. (Unlike the chat module — which is
*off* until enabled — this flag is *on* until explicitly disabled, so leaving
the variable unset keeps today's behavior.)
"""
from __future__ import annotations

import os

#: Spellings of "OTLP off". An unset/empty variable is NOT in this set, so the
#: listener stays on by default.
DISABLED_VALUES = frozenset({"off", "disabled", "disable", "false", "no", "none", "0"})


def is_disabled(value: str | None) -> bool:
    return (value or "").strip().lower() in DISABLED_VALUES


#: The OTLP listener is on unless explicitly switched off.
ENABLED = not is_disabled(os.environ.get("SIMPLEAUDIT_OTLP"))
