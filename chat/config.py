"""What the chat module is configured to do, and who Studio says you are.

Disabled unless SIMPLEAUDIT_CHAT is set.

Open WebUI serves from the root of an origin only — it has no base-path/sub-path
setting, and its HTML references ``/static``, ``/api`` and ``/ws`` absolutely. So
it cannot be reverse-proxied under ``/chat/`` on Studio's own origin. Instead it
runs on its own port and Studio embeds that origin in an iframe.

Single sign-on uses Open WebUI's trusted-header mode: a proxy in front of it asks
Studio who the browser is (``GET /chat/authz`` — the standard forward-auth
contract that Caddy/Traefik/nginx implement) and injects the answer as headers.
Django stays the only authority on identity; nothing reads Django's session or
user tables from outside.

    browser ──► proxy ──► GET /chat/authz  (cookies forwarded)
                  │         401 -> send the browser to Studio's login
                  │         200 -> X-Studio-Email / -Name / -Role
                  └──► Open WebUI on 127.0.0.1:8080

SECURITY: Open WebUI must be reachable only from that proxy. Anyone who can
connect to it directly can send ``X-Studio-Role: admin`` and take over the
instance. Bind it to loopback (embedded mode) or keep it on an internal compose
network with no published port (docker mode).

Modes (``SIMPLEAUDIT_CHAT``):
    embedded  the CLI starts Open WebUI and the proxy in chat.proxy
    docker    an external proxy (Caddy) does forward-auth; Studio only serves
              the iframe page and /chat/authz
    off       the URLs 404 and nothing starts — also "disabled", "false", "no",
              "0", or leaving the variable unset, which is the default
"""
from __future__ import annotations

import os

# --- Configuration ---------------------------------------------------------
#: Spellings of "no chat", so nobody has to guess which one this reads.
DISABLED_VALUES = frozenset({"", "off", "disabled", "disable", "false", "no", "none", "0"})

def is_disabled(value: str | None) -> bool:
    return (value or "").strip().lower() in DISABLED_VALUES


MODE = (os.environ.get("SIMPLEAUDIT_CHAT") or "").strip().lower()
ENABLED = not is_disabled(MODE)

#: Where Open WebUI itself listens. Never exposed to browsers.
UPSTREAM = os.environ.get("SIMPLEAUDIT_CHAT_UPSTREAM", "http://127.0.0.1:8080").rstrip("/")
#: The port the bundled forward-auth proxy listens on (embedded mode).
PROXY_PORT = int(os.environ.get("SIMPLEAUDIT_CHAT_PROXY_PORT", "8801"))
#: What the iframe points at — the proxy's origin, as the browser sees it. When
#: it is not configured, ``public_url(request)`` derives it from the page's own
#: host, because the host has to match for the session cookie to be sent.
PUBLIC_URL = (os.environ.get("SIMPLEAUDIT_CHAT_URL") or "").rstrip("/")

EMAIL_HEADER = "X-Studio-Email"
NAME_HEADER = "X-Studio-Name"
ROLE_HEADER = "X-Studio-Role"
#: Every header the proxy injects — it must strip all of them off the incoming
#: request before adding its own, or a client could forge them.
TRUSTED_HEADERS = (EMAIL_HEADER, NAME_HEADER, ROLE_HEADER)

# --- OTLP: Open WebUI exporting its spans to Studio --------------------------
#: When true, Open WebUI is started with OpenTelemetry tracing enabled and
#: pointed at Studio's own OTLP listener (``POST /otlp/v1/traces``), so the
#: spans it emits land in the same place as any other target's. Off by default:
#: the listener is a Studio feature that is only useful once an OTLP credential
#: (or an enabled "none" credential) exists to receive the spans.
OTLP_ENABLED = (os.environ.get("SIMPLEAUDIT_CHAT_OTLP") or "").strip().lower() in {
    "1", "true", "yes", "on",
}
#: Where the OTLP listener lives. Defaults to Studio's own web origin on
#: loopback; override for a non-default port or a separate collector. Must be
#: the *full* trace-ingestion URL — Open WebUI passes the endpoint to its
#: OTLP exporter explicitly, so the exporter uses it as-is (no path is
#: appended by the exporter).
OTLP_ENDPOINT = (os.environ.get("SIMPLEAUDIT_CHAT_OTLP_ENDPOINT") or "").rstrip("/")
#: The service name Open WebUI tags its spans with.
OTLP_SERVICE_NAME = os.environ.get("SIMPLEAUDIT_CHAT_OTLP_SERVICE_NAME", "open-webui")


def otlp_endpoint_url(studio_port: int | None = None) -> str:
    """The full OTLP trace-ingestion URL Open WebUI should export spans to.

    ``OTLP_ENDPOINT`` wins when set. Otherwise it is Studio's own web origin
    on loopback — the same host the proxy and the browser use — so the spans
    reach the ``/otlp/v1/traces`` listener on this Studio instance.

    The URL must include the ``/otlp/v1/traces`` path: Open WebUI passes the
    configured endpoint to its OTLP exporter explicitly (backend
    ``utils/telemetry/setup.py``), and an explicit endpoint is used as-is —
    the exporter appends nothing.
    """
    if OTLP_ENDPOINT:
        return OTLP_ENDPOINT
    port = studio_port or int(os.environ.get("PORT", "8000"))
    return f"http://127.0.0.1:{port}/otlp/v1/traces"


def public_url(request=None) -> str:
    """The origin the browser should load the chat from.

    SIMPLEAUDIT_CHAT_URL wins (a deployment behind TLS or on its own hostname
    knows better than we do). Otherwise it is the host the browser is already on,
    with the proxy's port: cookies are per host, not per port, so a page served
    from 127.0.0.1 must embed 127.0.0.1 and one served from localhost must embed
    localhost — otherwise the proxy gets no session cookie and bounces the iframe
    back to Studio.
    """
    if PUBLIC_URL:
        return PUBLIC_URL
    if request is None:
        return f"http://localhost:{PROXY_PORT}"
    host = request.get_host().split(":")[0]
    return f"{request.scheme}://{host}:{PROXY_PORT}"


def identity(user) -> dict[str, str]:
    """The trusted headers describing a signed-in Studio user.

    Role maps onto Open WebUI's three values: a Studio superuser, or an admin of
    any workspace, is an Open WebUI admin; everyone else is a user.
    """
    from accounts.models import ProjectMembership

    is_admin = user.is_superuser or ProjectMembership.objects.filter(
        user=user, role=ProjectMembership.Role.ADMIN,
    ).exists()
    return {
        # Open WebUI keys accounts by email, so a user without one still needs a
        # stable, unique value.
        EMAIL_HEADER: user.email or f"{user.username}@studio.local",
        NAME_HEADER: user.get_full_name() or user.username,
        ROLE_HEADER: "admin" if is_admin else "user",
    }
