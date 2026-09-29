"""The two things Studio serves for chat: the page, and who the browser is.

Everything about *why* it works this way is in chat/config.py.
"""
from __future__ import annotations

from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

# The module, not the names: tests and runtime both read ENABLED/PUBLIC_URL as
# they are now, not as they were at import time.
from chat import config


def authz(request):
    """Forward-auth endpoint: who is this browser?

    200 with the trusted headers when signed in, 401 otherwise. The proxy copies
    the headers onto the upstream request and turns a 401 into a redirect to
    Studio's login page.
    """
    if not config.ENABLED:
        raise Http404
    user = request.user
    if not user.is_authenticated:
        return HttpResponse(status=401)
    response = HttpResponse(status=200)
    for header, value in config.identity(user).items():
        response[header] = value
    return response


class ChatView(TemplateView):
    """The Studio page that embeds Open WebUI."""

    template_name = "chat/chat.html"

    def get(self, request, *args, **kwargs):
        if not config.ENABLED:
            raise Http404
        if not request.user.is_authenticated:
            return redirect(f"/login/?next={request.path}")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(chat_url=config.public_url(self.request), **kwargs)
