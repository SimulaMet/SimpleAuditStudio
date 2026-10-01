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


def chat_with(request, connection_id, model_id):
    """Hand off from /connections/ to the chat, pinned to one model.

    The icon links here (not straight to /chat/?model=) so the model is
    validated server-side and carried in the session, where ChatView consumes
    it once. A hand-typed ?model= on /chat/ is ignored — only a model the user
    can actually see, chosen through this view, can pin the chat.

    The model is identified by its connection plus its bare model id, because
    the same model id can be registered under more than one connection. The
    session stores the prefixed id (``<connection id>.<model id>``) that Open
    WebUI expects once Studio pushes a ``prefix_id`` per connection.
    """
    if not config.ENABLED:
        raise Http404
    if not request.user.is_authenticated:
        return redirect(f"/login/?next={request.path}")
    model_id = (model_id or "").strip()
    if model_id and _user_can_see_model(request, connection_id, model_id):
        request.session["chat_pinned_model"] = f"{connection_id}.{model_id}"
    return redirect(reverse("chat"))


def _user_can_see_model(request, connection_id, model_id) -> bool:
    """True if the user has a visible, enabled connection serving model_id.

    The connection must be one the user can see in this workspace, enabled, and
    actually serving the bare model id — so a hand-typed id for a connection the
    user cannot see is rejected even if the bare id exists elsewhere.
    """
    from model_registry.services import visible_connections_for

    project = getattr(request, "project", None)
    if project is None:
        return False
    conn = next(
        (c for c in visible_connections_for(request.user, project) if c.id == connection_id),
        None,
    )
    if conn is None or not conn.enabled or not (conn.base_url or "").strip():
        return False
    return conn.models.filter(enabled=True, model_id=model_id).exists()


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
        # hand-typed ?model= can never pin the chat. Otherwise the user's saved
        # preference, or the first model they can see.
        pinned = self.request.session.pop("chat_pinned_model", None) or self._resolve_default_model()
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
        groups = self._chat_model_groups()
        # The full-page "no models" state is only for a user with no project at
        # all (request.project is None) — that is the case where the chat is
        # genuinely unusable and there is no workspace to point at. A user who
        # has a project but no models yet still gets the normal page: the
        # picker shows its own "No models yet" hint and the iframe loads.
        has_project = getattr(self.request, "project", None) is not None
        context = {
            "chat_url": chat_url,
            "chat_model_data": {
                "base": chat_base,
                "groups": groups,
                "defaults": [m.strip() for m in pinned.split(",") if m.strip()],
            },
            "has_models": has_project,
        }
        if not has_project:
            context["no_models_message"] = (
                "No models are available for this workspace yet. "
                "Register a connection to start chatting."
            )
        return super().get_context_data(**context, **kwargs)

    def _visible_models(self):
        """The models the user can chat with, as ``{"id", "name", "has_key"}``.

        Same visibility rule as the experiments page: this workspace's own
        connections plus any shared into it, each enabled and reachable. Each
        id is the prefixed id (``<connection id>.<model id>``) — that is what
        Open WebUI's ?model=/ ?models= params expect once Studio pushes a
        ``prefix_id`` per connection, and it is what disambiguates the same
        model id under two connections.
        """
        from chat.api import chat_model_prefix
        from model_registry.services import visible_connections_for

        project = getattr(self.request, "project", None)
        if project is None:
            return []
        models = []
        for conn in visible_connections_for(self.request.user, project):
            if not conn.enabled or not (conn.base_url or "").strip():
                continue
            prefix = chat_model_prefix(conn)
            models.extend(
                {"id": f"{prefix}.{m.model_id}", "name": m.display_name, "has_key": m.has_key}
                for m in conn.models.filter(enabled=True)
            )
        return models

    def _chat_model_groups(self):
        """The models the top-bar picker offers, grouped by connection."""
        from chat.api import chat_model_prefix
        from model_registry.services import visible_connections_for

        project = getattr(self.request, "project", None)
        if project is None:
            return []
        groups = []
        for conn in visible_connections_for(self.request.user, project):
            if not conn.enabled or not (conn.base_url or "").strip():
                continue
            prefix = chat_model_prefix(conn)
            models = [
                {"id": f"{prefix}.{m.model_id}", "name": m.display_name, "has_key": m.has_key}
                for m in conn.models.filter(enabled=True)
            ]
            if models:
                groups.append({"connection": conn.name, "models": models})
        return groups

    def _resolve_default_model(self):
        """The model(s) to pin when no model was chosen through the handoff.

        The user's saved preference wins if it still names models they can see
        (a connection may have been deleted or a model removed since) — the
        saved value is the comma-separated selection, and any ids that are no
        longer available are dropped. Otherwise the first available model is
        pinned, so the chat opens on something usable rather than an empty
        picker. If the user has no visible models at all, nothing is pinned —
        Open WebUI shows its own default.
        """
        available = self._visible_models()
        if not available:
            return ""
        available_ids = {m["id"] for m in available}
        saved = (self.request.user.preferences or {}).get("chat_model")
        if saved:
            saved_ids = [i for i in (s.strip() for s in saved.split(",")) if i in available_ids]
            if saved_ids:
                return ",".join(saved_ids)
        return available[0]["id"]
