"""Personal API keys for Studio's OpenAI-compatible model passthrough.

A user issues one key (``sa_api_…``) that external clients present as
``Authorization: ****** to call ``/api/v1/models`` and
``/api/v1/chat/completions``. The key acts as its owner: requests are scoped to
the models the owner can see, and completions run through Open WebUI as that
user.

Only a salted hash of the token is stored — the plaintext is returned once at
creation (or rotation) and cannot be read back. Mirrors ``otlp_services``.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from django.db import transaction

# The hashing primitive is shared with the OTLP credential so both use the same
# salted SHA-256 + constant-time verify. We do NOT reuse generate_token here:
# that one hard-codes the "sa_otlp_" prefix, which would let an OTLP credential
# authenticate against the model API (and vice versa).
from simpleaudit.tracing.auth import (
    hash_secret,
    new_salt,
    token_lookup_prefix,
    verify_secret,
)

from model_registry.models import ApiCredential

#: Distinct from OTLP's "sa_otlp_" so the two credential spaces never overlap.
API_TOKEN_PREFIX = "sa_api_"


def _generate_api_token() -> str:
    return API_TOKEN_PREFIX + secrets.token_urlsafe(32)


@dataclass
class NewApiCredential:
    """A freshly created/rotated credential plus its one-time plaintext token."""

    credential: ApiCredential
    secret: str  # the bearer token, shown exactly once


@transaction.atomic
def upsert_credential(*, project, user) -> NewApiCredential:
    """Create the user's API key, or rotate it if one already exists.

    OWUI-style semantics: a user holds exactly one active key, and issuing a
    new one replaces the old (which stops working immediately). Returns the
    plaintext token exactly once.
    """
    cred = ApiCredential.objects.filter(user=user).first()
    if cred is not None:
        return _reissue(cred)

    salt = new_salt()
    secret = _generate_api_token()
    cred = ApiCredential(
        project=project,
        user=user,
        salt=salt,
        secret_hash=hash_secret(secret, salt),
        token_prefix=token_lookup_prefix(secret),
        enabled=True,
    )
    cred.save()
    return NewApiCredential(credential=cred, secret=secret)


def _reissue(cred: ApiCredential) -> NewApiCredential:
    salt = new_salt()
    secret = _generate_api_token()
    cred.salt = salt
    cred.secret_hash = hash_secret(secret, salt)
    cred.token_prefix = token_lookup_prefix(secret)
    cred.enabled = True
    cred.save(update_fields=["salt", "secret_hash", "token_prefix", "enabled", "updated_at"])
    return NewApiCredential(credential=cred, secret=secret)


def revoke_credential(user) -> bool:
    """Disable the user's API key. Returns True if a key was revoked."""
    cred = ApiCredential.objects.filter(user=user).first()
    if cred is None:
        return False
    cred.enabled = False
    cred.save(update_fields=["enabled", "updated_at"])
    return True


def verify_bearer(token: str, *, project=None) -> ApiCredential | None:
    """Resolve a presented bearer token to an enabled credential, else None.

    Indexed by ``token_prefix`` first (so we never hash against every row), then
    the salted hash is verified in constant time. ``project`` narrows the lookup
    when the caller knows the active workspace; omitted means any workspace.
    """
    if not token:
        return None
    qs = ApiCredential.objects.filter(token_prefix=token_lookup_prefix(token), enabled=True)
    if project is not None:
        qs = qs.filter(project=project)
    for cred in qs.select_related("user"):
        if verify_secret(token, cred.salt, cred.secret_hash):
            return cred
    return None
