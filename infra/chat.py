"""Optional Open WebUI chat module. Disabled unless SIMPLEAUDIT_CHAT is set.

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
    embedded  the CLI starts Open WebUI and the proxy in infra.chat_proxy
    docker    an external proxy (Caddy) does forward-auth; Studio only serves
              the iframe page and /chat/authz
    off       the URLs 404 and nothing starts — also "disabled", "false", "no",
              "0", or leaving the variable unset, which is the default
"""
from __future__ import annotations

import os

from django.http import Http404, HttpResponse
from django.views.generic import TemplateView

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
#: What the iframe points at — the proxy's origin, as the browser sees it.
PUBLIC_URL = os.environ.get("SIMPLEAUDIT_CHAT_URL", f"http://localhost:{PROXY_PORT}").rstrip("/")

EMAIL_HEADER = "X-Studio-Email"
NAME_HEADER = "X-Studio-Name"
ROLE_HEADER = "X-Studio-Role"
#: Every header the proxy injects — it must strip all of them off the incoming
#: request before adding its own, or a client could forge them.
TRUSTED_HEADERS = (EMAIL_HEADER, NAME_HEADER, ROLE_HEADER)


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


def authz(request):
    """Forward-auth endpoint: who is this browser?

    200 with the trusted headers when signed in, 401 otherwise. The proxy copies
    the headers onto the upstream request and turns a 401 into a redirect to
    Studio's login page.
    """
    if not ENABLED:
        raise Http404
    user = request.user
    if not user.is_authenticated:
        return HttpResponse(status=401)
    response = HttpResponse(status=200)
    for header, value in identity(user).items():
        response[header] = value
    return response


class ChatView(TemplateView):
    """The Studio page that embeds Open WebUI."""

    template_name = "chat.html"

    def get(self, request, *args, **kwargs):
        if not ENABLED:
            raise Http404
        if not request.user.is_authenticated:
            from django.shortcuts import redirect

            return redirect(f"/login/?next={request.path}")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(chat_url=PUBLIC_URL, **kwargs)
