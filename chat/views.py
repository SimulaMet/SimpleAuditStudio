"""The two things Studio serves for chat: the page, and who the browser is.

Everything about *why* it works this way is in chat/config.py.
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
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


def chat_with(request, model_id):
    """Hand off from /connections/ to the chat, pinned to one model.

    The icon links here (not straight to /chat/?model=) so the model is
    validated server-side and carried in the session, where ChatView consumes
    it once. A hand-typed ?model= on /chat/ is ignored — only a model the user
    can actually see, chosen through this view, can pin the chat.
    """
    if not config.ENABLED:
        raise Http404
    if not request.user.is_authenticated:
        return redirect(f"/login/?next={request.path}")
    model_id = (model_id or "").strip()
    if model_id and _user_can_see_model(request, model_id):
        request.session["chat_pinned_model"] = model_id
    return redirect(reverse("chat"))


def _user_can_see_model(request, model_id) -> bool:
    """True if the user has a visible, enabled connection serving model_id."""
    from model_registry.services import visible_connections_for

    project = getattr(request, "project", None)
    if project is None:
        return False
    for conn in visible_connections_for(request.user, project):
        if not conn.enabled or not (conn.base_url or "").strip():
            continue
        if conn.models.filter(enabled=True, model_id=model_id).exists():
            return True
    return False


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
        # Which model(s) to pin. A model chosen through the /connections chat
        # icon is stashed in the session by chat_with and consumed here exactly
        # once (pop) — so it survives the redirect but not a refresh, and a
        # hand-typed ?model= can never pin the chat. Falls back to the default.
        pinned = self.request.session.pop("chat_pinned_model", None) or config.MODEL
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
        if pinned:
            params["models"] = pinned
        params["temporary-chat"] = "true"
        chat_url = f"{chat_base}?{urlencode(params)}"
        return super().get_context_data(
            chat_url=chat_url,
            chat_model_data={
                "base": chat_base,
                "groups": self._chat_model_groups(),
                "defaults": [m.strip() for m in pinned.split(",") if m.strip()],
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
