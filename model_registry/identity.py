"""Model identity: what a registered model *is*, independent of how it is served.

A ``RegisteredModel`` is addressed by its provider-specific ``model_id``
(e.g. ``qwen3-30b`` on Sigma2), which rarely says which base model sits behind
it. ``model_identity`` records that, so pages can link to a model card and,
later, join benchmark results:

- ``hf``: open weights on Hugging Face; ``id`` is the repo id
  (``Qwen/Qwen3-30B-A3B``) and the card URL is derived from it.
- ``vendor``: a commercial API model (``gpt-4.1``); ``url`` points at the
  vendor's model or system card.
- ``system``: an external RAG/agent endpoint; ``url`` points at its own card,
  ``id`` optionally names the base model if known.

``benchmark_id`` (optional) is the name a benchmark leaderboard uses for the
model when it differs from ``id`` (e.g. ``gpt-4.1-2025-04-14`` on EuroEval).
The leaderboard link comes from the admin's ``leaderboard_url_template``.

Agents need no identity of their own: they inherit their ``base_model``'s.
The identity is copied into run snapshots, so remapping a model later never
rewrites what past runs show.
"""
from __future__ import annotations

import re
from urllib.parse import quote

KINDS = ("hf", "vendor", "system")
KIND_LABELS = {"hf": "Hugging Face", "vendor": "Commercial", "system": "RAG / system"}

# owner/name, Hugging Face's repo-id charset.
_HF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
ID_MAX = 250
URL_MAX = 500


class IdentityError(ValueError):
    """Raised for an identity that cannot be stored."""


def normalize_identity(kind: str, ident: str = "", url: str = "", benchmark_id: str = "") -> dict:
    """Validate form/API input into the stored ``model_identity`` dict.

    An empty ``kind`` clears the identity (returns ``{}``).
    """
    kind = (kind or "").strip()
    ident = (ident or "").strip()
    url = (url or "").strip()
    benchmark_id = (benchmark_id or "").strip()
    if not kind:
        return {}
    if kind not in KINDS:
        raise IdentityError(f"Unknown identity type “{kind}”.")
    if len(ident) > ID_MAX:
        raise IdentityError(f"Identifier is too long ({ID_MAX} characters at most).")
    if len(url) > URL_MAX:
        raise IdentityError(f"Card URL is too long ({URL_MAX} characters at most).")
    if len(benchmark_id) > ID_MAX:
        raise IdentityError(f"Benchmark ID is too long ({ID_MAX} characters at most).")
    if url and not re.match(r"^https?://[^\s/]+", url, re.IGNORECASE):
        raise IdentityError("Card URL must start with http:// or https://.")
    if kind == "hf":
        if ident.lower().startswith(("https://huggingface.co/", "http://huggingface.co/")):
            ident = ident.split("huggingface.co/", 1)[1].strip("/")
        if not _HF_ID_RE.match(ident):
            raise IdentityError("Hugging Face identifier must look like “owner/model”, e.g. Qwen/Qwen3-30B-A3B.")
    elif not ident and not url:
        raise IdentityError("Give a model name or a card URL.")
    out = {"kind": kind}
    if ident:
        out["id"] = ident
    if url:
        out["url"] = url
    if benchmark_id:
        out["benchmark_id"] = benchmark_id
    return out


def card_url(identity: dict | None) -> str:
    """The model-card link for an identity: explicit ``url`` wins, HF is derived."""
    if not isinstance(identity, dict):
        return ""
    url = str(identity.get("url") or "")
    if url.lower().startswith(("http://", "https://")):
        return url
    if identity.get("kind") == "hf" and _HF_ID_RE.match(str(identity.get("id") or "")):
        return f"https://huggingface.co/{identity['id']}"
    return ""


def identity_view(identity: dict | None) -> dict | None:
    """Template-ready view (kind label, id, link), or ``None`` when unset."""
    if not isinstance(identity, dict) or identity.get("kind") not in KINDS:
        return None
    return {
        "kind": identity["kind"],
        "kind_label": KIND_LABELS[identity["kind"]],
        "id": str(identity.get("id") or ""),
        "card_url": card_url(identity),
        "benchmark_key": benchmark_key(identity),
    }


def benchmark_key(identity: dict | None) -> str:
    """The name to look the model up by on a leaderboard: benchmark_id, else id."""
    if not isinstance(identity, dict):
        return ""
    return str(identity.get("benchmark_id") or identity.get("id") or "").strip()


PLACEHOLDER = "{model}"


def validate_leaderboard_template(value) -> str:
    """Clean an admin-entered leaderboard URL template ('' disables the link)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise IdentityError("Leaderboard URL must be text.")
    value = value.strip()
    if not value:
        return ""
    if len(value) > URL_MAX:
        raise IdentityError(f"Leaderboard URL is too long ({URL_MAX} characters at most).")
    if not re.match(r"^https?://[^\s/]+", value, re.IGNORECASE) or any(c.isspace() for c in value):
        raise IdentityError("Leaderboard URL must start with http:// or https:// and contain no spaces.")
    if PLACEHOLDER not in value:
        raise IdentityError(f"Leaderboard URL must contain {PLACEHOLDER}, where the model name goes.")
    return value


def leaderboard_url(template: str, identity: dict | None) -> str:
    """Fill the template with the model's benchmark key ('' when either is missing)."""
    key = benchmark_key(identity)
    if not template or not key or PLACEHOLDER not in template:
        return ""
    # Percent-encode the name and any literal quotes so the href stays well formed.
    return template.replace(PLACEHOLDER, quote(key, safe="/")).replace('"', "%22")
