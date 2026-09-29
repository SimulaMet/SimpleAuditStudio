"""The two things Studio serves for chat: the page, and who the browser is.

Everything about *why* it works this way is in chat/config.py.
"""
from __future__ import annotations

from urllib.parse import urlencode

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
        chat_base = config.public_url(self.request)
        # Shape the embedded chat through URL params, which Open WebUI reads on
        # load:
        #   ?models=         pin to one or more models, comma-separated (the
        #                    in-frame picker is hidden, so this is the only way
        #                    to choose them). The top-bar picker rebuilds this.
        #   ?temporary-chat  start in temporary mode, so nothing is saved to the
        #                    chat history. The embed is a throwaway surface.
        # The New Chat button is hidden, so a fresh chat only ever starts from a
        # full page load — which re-reads these params — so the param is enough.
        params = {}
        if config.MODEL:
            params["models"] = config.MODEL
        params["temporary-chat"] = "true"
        chat_url = f"{chat_base}?{urlencode(params)}"
        return super().get_context_data(
            chat_url=chat_url,
            chat_model_data={
                "base": chat_base,
                "groups": self._chat_model_groups(),
                "defaults": [m.strip() for m in config.MODEL.split(",") if m.strip()],
            },
            **kwargs,
        )

    def _chat_model_groups(self):
        """The models the top-bar picker offers, grouped by connection.

        Same visibility rule as the experiments page: this workspace's own
        connections plus any shared into it. Each value is the raw model_id —
        that is what Open WebUI's ?model=/ ?models= params expect for
        OpenAI-compatible connections.
        """
        from model_registry.services import visible_connections_for

        project = getattr(self.request, "project", None)
        if project is None:
            return []
        groups = []
        for conn in visible_connections_for(self.request.user, project):
            if not conn.enabled or not (conn.base_url or "").strip():
                continue
            models = [
                {"id": m.model_id, "name": m.display_name, "has_key": m.has_key}
                for m in conn.models.filter(enabled=True)
            ]
            if models:
                groups.append({"connection": conn.name, "models": models})
        return groups
