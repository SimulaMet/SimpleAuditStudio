"""What the chat module is configured to do, and who Studio says you are.

Disabled unless SIMPLEAUDIT_CHAT is set.

Single-origin model (since the subpath fork): Open WebUI is built with
``WEBUI_SUBPATH=/chat`` so it serves its entire app — HTML, ``/_app``,
``/static``, ``/api``, ``/ws`` — under ``/chat/`` on Studio's own origin. A
Caddy in front (``deploy/openwebui-subpath/Caddyfile.subpath``) routes
``/chat/*`` to it and does forward-auth against ``GET /chat/authz``; everything
else goes to Studio. The browser sees one origin and one port.

    browser ──► Caddy ──► /chat/* ──► forward_auth GET /chat/authz (cookies)
                              │         401 -> 302 to Studio's /login/
                              │         200 -> X-Studio-Email / -Name / -Role
                              └──► Open WebUI on the internal :8080 (at /chat)

Studio's own wrapper page (the model-picker bar) lives at ``/playground/`` and iframes
``/chat/``; both are same-origin now.

Single sign-on uses Open WebUI's trusted-header mode: the proxy injects the
answer of /chat/authz as headers. Django stays the only authority on
identity; nothing reads Django's session or user tables from outside.

SECURITY: Open WebUI must be reachable only through that proxy. Anyone who can
connect to it directly can send ``X-Studio-Role: admin`` and take over the
instance. Bind it to loopback (embedded mode) or keep it on an internal compose
network with no published port (docker mode).

Modes (``SIMPLEAUDIT_CHAT``):
    embedded  the CLI starts Open WebUI (the subpath build, launched with
              ``WEBUI_SUBPATH=/chat``) and the front door on PUBLIC_PORT —
              the same public port the docker deployment uses. Studio's web
              server moves to the INTERNAL_PORT while the front door is up,
              so the browser sees one port, exactly like docker. The front
              door is the bundled Caddy binary (caddyserver wheel, all
              platforms) — the same Caddyfile shape as
              deploy/openwebui-subpath/Caddyfile.subpath. A missing binary
              (a broken install) fails the front door at startup.
    docker    Caddy (this compose) does forward-auth; Studio only serves the
              /playground/ wrapper page, the admin iframes and /chat/authz
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

#: The base path the Open WebUI subpath build serves at. Must match the
#: WEBUI_SUBPATH baked into the image (0.11.4.1-subpath -> /chat). Defined
#: first: UPSTREAM below bakes it into the default.
SUBPATH = os.environ.get("SIMPLEAUDIT_CHAT_SUBPATH", "/chat")
if not SUBPATH.startswith("/"):
    SUBPATH = "/" + SUBPATH
SUBPATH = SUBPATH.rstrip("/")

#: Where Open WebUI itself listens. Never exposed to browsers.
#:
#: BOTH modes include the subpath: the subpath build (WEBUI_SUBPATH=/chat)
#: mounts every route (API, socket, static, the SPA) at /chat/... on its own
#: server. In compose that is
#: SIMPLEAUDIT_CHAT_UPSTREAM=http://open-webui:8080/chat; embedded mode uses
#: the loopback default below.
UPSTREAM = os.environ.get(
    "SIMPLEAUDIT_CHAT_UPSTREAM", "http://127.0.0.1:8080" + SUBPATH,
).rstrip("/")
#: The one public port the browser sees. In embedded mode the front door
#: (bundled Caddy, or the Python fallback) binds it and Studio's web server
#: moves to INTERNAL_PORT. Defaults to 8000 — the docker deployment's port.
PUBLIC_PORT = int(os.environ.get("SIMPLEAUDIT_CHAT_PUBLIC_PORT", "8000"))
#: Where Studio's web server listens while the front door owns PUBLIC_PORT.
INTERNAL_PORT = int(os.environ.get("SIMPLEAUDIT_CHAT_INTERNAL_PORT", "8001"))
#: What the iframe points at. Since the subpath cutover this is the same
#: origin as Studio, so the default is a base PATH, not an origin. Set
#: SIMPLEAUDIT_CHAT_URL to a full URL only if a deployment keeps OWUI on its
#: own origin. When unset, ``public_url(request)`` returns the subpath base.
PUBLIC_URL = (os.environ.get("SIMPLEAUDIT_CHAT_URL") or "").rstrip("/")

# How long an admin page's document load keeps the session on the admin embed
# assets (css_mask). The iframe URL carries embed=admin fresh on every render,
# so this only needs to cover the document-then-css gap within one load — it
# mirrors the proxy's ADMIN_MARKER_TTL window.
EMBED_ADMIN_FLAG_TTL = int(os.environ.get("SIMPLEAUDIT_CHAT_EMBED_ADMIN_TTL", "30"))

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


def is_embedded() -> bool:
    """True when the CLI runs Open WebUI + the bundled proxy in-process.

    Both modes are same-origin now: embedded puts the front door on PUBLIC_PORT
    (Studio moves to INTERNAL_PORT while it is up), docker puts Caddy in front
    — either way the iframe points at the subpath on Studio's own origin and
    the session cookie rides along.
    """
    return MODE == "embedded"


def public_url(request=None) -> str:
    """Where the browser loads the chat from — no trailing slash.

    SIMPLEAUDIT_CHAT_URL wins (a full origin, for a deployment that keeps OWUI
    on its own host).

    Otherwise the subpath base ON THIS ORIGIN — ``/chat`` by default — in
    every mode: the subpath build serves /chat/ on Studio's own origin, and
    the front door (Caddy in docker, the bundled front door on PUBLIC_PORT in
    embedded) owns /chat/* and does the forward-auth.
    """
    if PUBLIC_URL:
        return PUBLIC_URL
    return SUBPATH


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
