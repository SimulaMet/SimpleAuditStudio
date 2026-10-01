"""OTLP credential services for Studio.

Generates and verifies the credentials an external target uses to push OTLP
traces to Studio. The secret hashing/verification mirrors
``simpleaudit.tracing.auth`` (salted SHA-256 + constant-time compare) so the
library and Studio agree on the wire format.

Two auth modes:
    - ``basic``  — username + password (OpenWebUI's native OTEL_BASIC_AUTH_*).
    - ``bearer`` — a single token (generic OTel exporters via headers).

Only a salted hash of the secret is stored; the plaintext is returned once at
creation so the user can copy it into the target's environment.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from django.db import transaction

from model_registry.models import OTLPCredential

_SALT_LEN = 16


def _hash_secret(secret: str, salt: bytes) -> bytes:
    return hashlib.sha256(salt + secret.encode("utf-8")).digest()


def _new_salt() -> bytes:
    return os.urandom(_SALT_LEN)


def generate_password() -> str:
    """A strong, URL-safe password for a Basic-Auth credential."""
    return secrets.token_urlsafe(32)


_BEARER_TOKEN_PREFIX = "sa_otlp_"
# Length of the non-secret lookup prefix stored on a bearer credential.
_TOKEN_LOOKUP_PREFIX_LEN = 16


def generate_token() -> str:
    """A strong bearer token (prefixed for easy recognition in logs)."""
    return _BEARER_TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_lookup_prefix(token: str) -> str:
    """A short, non-secret prefix of a bearer token for indexed lookup.

    This is stored on the credential so ``verify_bearer`` can narrow the
    candidate set before hashing. It is deliberately short and never used for
    authentication (only the salted hash is).
    """
    return (token or "")[:_TOKEN_LOOKUP_PREFIX_LEN]


def _make_target_id(connection) -> str:
    """A stable, human-readable target id derived from the connection."""
    base = "".join(c for c in connection.name.lower() if c.isalnum())[:24] or "target"
    return f"{base}_{secrets.token_hex(4)}"


@dataclass
class NewCredential:
    """A freshly created credential plus its one-time plaintext secret."""

    credential: OTLPCredential
    secret: str  # password (basic) or token (bearer)


@transaction.atomic
def create_credential(*, project, connection, auth_mode: str, user=None) -> NewCredential:
    """Create a credential for ``connection`` and return it with its secret.

    ``auth_mode`` is ``"none"``, ``"basic"``, or ``"bearer"``. For ``none`` no
    secret is generated (the endpoint is open); for the other two the plaintext
    secret is generated here and returned once, with only its salted hash
    persisted.
    """
    valid = (OTLPCredential.AuthMode.NONE, OTLPCredential.AuthMode.BASIC, OTLPCredential.AuthMode.BEARER)
    if auth_mode not in valid:
        raise ValueError(f"Unknown OTLP auth mode: {auth_mode!r}")

    # Reuse the existing target_id if a credential already exists for this
    # connection, so the upsert updates in place rather than creating a new
    # target identity.
    existing = OTLPCredential.objects.filter(project=project, connection=connection).first()
    target_id = existing.target_id if existing else _make_target_id(connection)
    token_prefix = ""
    if auth_mode == OTLPCredential.AuthMode.NONE:
        username = ""
        secret = ""
        salt = None
        secret_hash = None
    elif auth_mode == OTLPCredential.AuthMode.BASIC:
        username = f"sa_{target_id}"
        secret = generate_password()
        salt = _new_salt()
        secret_hash = _hash_secret(secret, salt)
    else:  # bearer
        username = ""
        secret = generate_token()
        salt = _new_salt()
        secret_hash = _hash_secret(secret, salt)
        token_prefix = token_lookup_prefix(secret)

    # Upsert: if a credential already exists for this connection, update it
    # in place (change auth mode, regenerate secret) instead of failing on
    # the unique constraint.
    cred, _created = OTLPCredential.objects.update_or_create(
        project=project,
        target_id=target_id,
        defaults={
            "connection": connection,
            "auth_mode": auth_mode,
            "username": username,
            "secret_hash": secret_hash,
            "salt": salt,
            "token_prefix": token_prefix if auth_mode == OTLPCredential.AuthMode.BEARER else "",
            "enabled": True,
            "created_by": user,
        },
    )
    return NewCredential(credential=cred, secret=secret)


@transaction.atomic
def rotate_credential(cred: OTLPCredential) -> NewCredential:
    """Re-issue the secret for an existing credential (same target_id).

    The old secret stops working immediately. Returns the credential plus the
    new one-time plaintext secret. Only meaningful for ``basic``/``bearer`` —
    a ``none`` credential has no secret to rotate.
    """
    if cred.auth_mode == OTLPCredential.AuthMode.NONE:
        raise ValueError("A 'none' credential has no secret to rotate.")

    if cred.auth_mode == OTLPCredential.AuthMode.BASIC:
        secret = generate_password()
    else:  # bearer
        secret = generate_token()

    salt = _new_salt()
    cred.secret_hash = _hash_secret(secret, salt)
    cred.salt = salt
    cred.token_prefix = token_lookup_prefix(secret)
    cred.enabled = True  # rotation re-enables a revoked credential
    cred.save(update_fields=["secret_hash", "salt", "token_prefix", "enabled", "updated_at"])
    return NewCredential(credential=cred, secret=secret)


def verify_basic(username: str, password: str) -> OTLPCredential | None:
    """Return the enabled credential matching ``username``/``password``, else None."""
    cred = OTLPCredential.objects.filter(
        username=username, auth_mode=OTLPCredential.AuthMode.BASIC, enabled=True
    ).first()
    if cred is None:
        return None
    if hmac.compare_digest(cred.secret_hash, _hash_secret(password, cred.salt)):
        return cred
    return None


def verify_bearer(token: str) -> OTLPCredential | None:
    """Return the enabled bearer credential matching ``token``, else None.

    The token's short lookup prefix narrows the candidate set (an indexed
    query), then the salted hash is verified in constant time. Falls back to
    scanning all bearer credentials when the prefix is unset (e.g. rows
    created before the ``token_prefix`` field existed) so verification never
    regresses.
    """
    prefix = token_lookup_prefix(token)
    qs = OTLPCredential.objects.filter(
        auth_mode=OTLPCredential.AuthMode.BEARER, enabled=True
    ).only("salt", "secret_hash", "target_id", "id", "token_prefix")
    # Prefer the indexed prefix match; if none is stored, scan the (small) set.
    candidates = qs.filter(token_prefix=prefix)
    if not candidates.exists():
        candidates = qs
    for cred in candidates:
        if cred.salt and hmac.compare_digest(cred.secret_hash, _hash_secret(token, cred.salt)):
            return cred
    return None


def parse_basic_header(authorization: str | None) -> tuple[str, str] | None:
    """Parse an ``Authorization: Basic ...`` header into (username, password).

    Returns None when the header is missing, not Basic, or not valid base64.
    """
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def parse_bearer_header(authorization: str | None) -> str | None:
    """Parse an ``Authorization: Bearer ...`` header into the token, else None."""
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def otlp_env_vars(*, endpoint: str, username: str, password: str) -> list[tuple[str, str]]:
    """Env vars for a target that authenticates with Basic Auth.

    These map to the standard ``OTEL_BASIC_AUTH_USERNAME`` / ``PASSWORD``
    variables that OpenTelemetry exporters (e.g. OpenWebUI's) convert into an
    ``Authorization: Basic ...`` header.
    """
    return [
        ("OTEL_OTLP_SPAN_EXPORTER", "http"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
        ("OTEL_BASIC_AUTH_USERNAME", username),
        ("OTEL_BASIC_AUTH_PASSWORD", password),
    ]


def otlp_bearer_env_vars(*, endpoint: str, token: str) -> list[tuple[str, str]]:
    """Env vars for a generic OTel exporter using a Bearer token."""
    return [
        ("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
        ("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization=Bearer {token}"),
    ]


def otlp_open_env_vars(*, endpoint: str) -> list[tuple[str, str]]:
    """Env vars for an unauthenticated (``none``) target — just the endpoint."""
    return [
        ("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
    ]
