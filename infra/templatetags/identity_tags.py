"""Template helpers for model identities (``model_registry.identity``)."""
import json
from urllib.parse import urlsplit

from django import template
from django.urls import reverse
from django.utils.html import format_html

from model_registry.identity import identity_view as _identity_view
from model_registry.identity import leaderboard_url as _leaderboard_url

register = template.Library()


@register.filter
def leaderboard_link(identity_view: dict | None, url_template) -> str:
    """Leaderboard URL for an identity view (from ``identity_view``), or ''."""
    if not identity_view or not url_template:
        return ""
    return _leaderboard_url(str(url_template), {"id": identity_view.get("benchmark_key", "")})


def embed_url(url: str) -> str:
    """The iframe form of a leaderboard URL (EuroEval drops its chrome with embed=1)."""
    if not url:
        return ""
    host = (urlsplit(url).hostname or "").lower()
    if host == "euroeval.com" or host.endswith(".euroeval.com"):
        return url + ("&" if "?" in url else "?") + "embed=1"
    return url


def model_payload(model, url_template: str = "", role: str = "", *, with_connection: bool = True) -> dict:
    """What the model modal shows, from a RegisteredModel or a frozen-model view dict."""
    if isinstance(model, dict):
        identity = model.get("identity") or _identity_view(model.get("model_identity"))
        name = model.get("display_name") or model.get("model_id") or ""
        model_id = model.get("model_id") or ""
        provider = model.get("provider") or ""
        connection_id = model.get("connection_id")
        frozen = True
    else:
        identity = _identity_view(model.model_identity)
        name, model_id = model.display_name or model.model_id, model.model_id
        provider = model.connection.provider
        connection_id = model.connection_id
        frozen = False
    lb = leaderboard_link(identity, url_template) if identity else ""
    return {
        "name": name,
        "modelId": model_id,
        "provider": provider,
        "role": role,
        "frozen": frozen,
        "kind": identity["kind"] if identity else "",
        "kindLabel": identity["kind_label"] if identity else "",
        "identityId": identity["id"] if identity else "",
        "benchmarkKey": identity["benchmark_key"] if identity else "",
        "cardUrl": identity["card_url"] if identity else "",
        "leaderboardUrl": lb,
        "leaderboardEmbedUrl": embed_url(lb),
        "connectionUrl": (
            f"{reverse('connections')}#conn-{connection_id}" if with_connection and connection_id else ""
        ),
    }


@register.simple_tag(takes_context=True)
def model_modal_attr(context, model, role: str = ""):
    """``data-model-modal="{json}"`` for a trigger that opens the shared model modal."""
    user = context.get("user")
    payload = model_payload(
        model, str(context.get("leaderboard_url_template") or ""), role,
        with_connection=bool(user and user.is_authenticated),
    )
    return format_html('data-model-modal="{}"', json.dumps(payload))
