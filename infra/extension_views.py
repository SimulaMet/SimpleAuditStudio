"""Extension endpoints: the embed page and the forward-auth check (issue #83).

Both 404 when no extension is configured, like the rest of the feature.
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponse
from django.shortcuts import render

from infra.extensions import get_extension, load_extensions, role_level, user_level


def embed_page(request, slug: str):
    """``/x/<slug>/``: the Studio shell around an iframe of the entry's ``embed`` URL.

    Unknown slugs, entries without ``embed`` and entries the user may not see
    all return 404, so the page does not reveal which extensions exist.
    """
    if not load_extensions():
        raise Http404
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
    ext = get_extension(slug, request.user)
    if ext is None or not ext.embed:
        raise Http404
    return render(request, "extension_embed.html", {
        "extension": ext,
        "nav_current": {"label": ext.label, "url": ext.url, "child": None},
        "page_title": ext.label,
    })


def authz(request):
    """``GET /ext/authz?role=<role>``: forward-auth for companion apps.

    Same contract as ``/chat/authz``: 200 with the ``X-Studio-*`` identity
    headers when the session holds ``role`` (default ``admin``) or higher, 401
    when signed out (``X-Studio-Login`` carries the login URL for the proxy's
    redirect), 403 when signed in below the role.
    """
    if not load_extensions():
        raise Http404
    required = role_level(request.GET.get("role", "admin"))
    if required is None:
        return HttpResponse("Unknown role.", status=400, content_type="text/plain")
    user = request.user
    if not user.is_authenticated:
        response = HttpResponse(status=401)
        # The proxy forwards the original URI; send the user back to it after login.
        nxt = request.headers.get("X-Forwarded-Uri", "")
        response["X-Studio-Login"] = settings.LOGIN_URL + (f"?{urlencode({'next': nxt})}" if nxt else "")
        return response
    if user_level(user) < required:
        return HttpResponse(status=403)
    # Same header names and email rules as /chat/authz.
    from chat.config import identity

    response = HttpResponse(status=200)
    for header, value in identity(user).items():
        response[header] = value
    response["X-Studio-Username"] = user.username
    response["Cache-Control"] = "no-store"
    return response
