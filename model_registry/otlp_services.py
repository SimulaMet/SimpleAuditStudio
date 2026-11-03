"""OTLP credential services for Studio.

Generates and verifies the credentials an external target uses to push OTLP
traces to Studio. The credential *primitive* — salted SHA-256 hashing,
constant-time verification, header parsing, and secret generation — lives in
the core (``simpleaudit.tracing.auth``) so the library and Studio share one
definition. This module adds the Studio persistence layer on top: the
``OTLPCredential`` table, per-connection lookup, and the "none" fallback.

Two auth modes:
    - ``basic``  — username + password (OpenWebUI's native OTEL_BASIC_AUTH_*).
    - ``bearer`` — a single token (generic OTel exporters via headers).

Only a salted hash of the secret is stored; the plaintext is returned once at
creation so the user can copy it into the target's environment.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

# The credential primitive (salted-hash + constant-time verify + header
# parsing + secret generation) lives in the core so the library and Studio
# share one definition. Studio adds the persistence layer on top: the
# OTLPCredential table, per-connection lookup, and the "none" fallback.
from simpleaudit.tracing.auth import (
    generate_password,
    generate_token,
    hash_secret,
    new_salt,
    token_lookup_prefix,
    verify_secret,
)

from model_registry.models import OTLPCredential, OtlpSpan, OtlpTraceClaim

logger = logging.getLogger(__name__)

# Span retention. A run claims each turn's trace id as the turn records it, so a
# held span only waits for its turn's claim (seconds, plus the turn's own length).
# Unclaimed traces (no run asked for them) are pruned after ``SPAN_HOLD_TTL``.
# Claimed traces are kept for ``CLAIMED_SPAN_RETENTION``.
SPAN_HOLD_TTL = timedelta(seconds=float(os.environ.get("SIMPLEAUDIT_OTLP_HOLD_SECONDS") or 600))
SPAN_HOLD_CAP = 20_000  # held (unclaimed) spans kept per target, newest first
CLAIMED_SPAN_RETENTION = timedelta(days=30)
PRUNE_INTERVAL_S = 60.0

_last_prune = 0.0


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
def create_credential(
    *, project, connection, auth_mode: str, user=None, capture_level: str = "structural",
    username: str | None = None, secret: str | None = None,
) -> NewCredential:
    """Create a credential for ``connection`` and return it with its secret.

    ``auth_mode`` is ``"none"``, ``"basic"``, or ``"bearer"``. For ``none`` no
    secret is generated (the endpoint is open); for the other two the plaintext
    secret is generated here and returned once, with only its salted hash
    persisted (plus a sealed copy for reveal).

    ``secret`` (and, for basic, ``username``) pin a known value instead of a
    generated one, for credentials Studio hands to a process it starts itself.
    """
    from audits.agentic.privacy import capture_level as normalize_capture_level

    valid = (OTLPCredential.AuthMode.NONE, OTLPCredential.AuthMode.BASIC, OTLPCredential.AuthMode.BEARER)
    if auth_mode not in valid:
        raise ValueError(f"Unknown OTLP auth mode: {auth_mode!r}")

    capture_level = normalize_capture_level(capture_level)
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
        username = username or f"sa_{target_id}"
        secret = secret or generate_password()
        salt = new_salt()
        secret_hash = hash_secret(secret, salt)
    else:  # bearer
        username = ""
        secret = secret or generate_token()
        salt = new_salt()
        secret_hash = hash_secret(secret, salt)
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
            "secret_encrypted": seal(secret) if secret else "",
            "salt": salt,
            "token_prefix": token_prefix if auth_mode == OTLPCredential.AuthMode.BEARER else "",
            "enabled": True,
            "capture_level": capture_level,
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

    salt = new_salt()
    cred.secret_hash = hash_secret(secret, salt)
    cred.salt = salt
    cred.token_prefix = token_lookup_prefix(secret)
    cred.secret_encrypted = seal(secret)
    cred.enabled = True  # rotation re-enables a revoked credential
    cred.save(update_fields=["secret_hash", "secret_encrypted", "salt", "token_prefix", "enabled", "updated_at"])
    return NewCredential(credential=cred, secret=secret)


def verify_basic(username: str, password: str) -> OTLPCredential | None:
    """Return the enabled credential matching ``username``/``password``, else None."""
    cred = OTLPCredential.objects.filter(
        username=username, auth_mode=OTLPCredential.AuthMode.BASIC, enabled=True
    ).first()
    if cred is None:
        return None
    if verify_secret(password, cred.salt, cred.secret_hash):
        return cred
    return None


def verify_bearer(token: str) -> OTLPCredential | None:
    """Return the enabled bearer credential matching ``token``, else None.

    One indexed query: the token's lookup prefix, plus rows with no stored
    prefix (created before the field existed). The salted hash is then verified
    in constant time. A token that matches no prefix costs no scan.
    """
    candidates = OTLPCredential.objects.filter(
        Q(token_prefix=token_lookup_prefix(token)) | Q(token_prefix=""),
        auth_mode=OTLPCredential.AuthMode.BEARER, enabled=True,
    )
    for cred in candidates:
        if verify_secret(token, cred.salt, cred.secret_hash):
            return cred
    return None


# ─── Sealed secrets (reveal) ────────────────────────────────────────────────


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"simpleaudit-otlp-secret:" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def reveal_secret(cred: OTLPCredential, *, user=None) -> str | None:
    """The credential's secret, or ``None`` if it has no readable copy.

    No copy exists for ``none`` credentials, for credentials issued before
    reveal existed, or after ``SECRET_KEY`` changed: rotate to get a new one.
    Every successful reveal is logged.
    """
    if not cred.secret_encrypted:
        return None
    try:
        secret = _fernet().decrypt(cred.secret_encrypted.encode()).decode()
    except InvalidToken:
        return None
    logger.info(
        "OTLP credential revealed: target=%s credential=%s user=%s",
        cred.target_id, cred.pk, getattr(user, "pk", None),
    )
    return secret


def credential_env_vars(cred: OTLPCredential, secret: str, endpoint: str) -> dict[str, str]:
    """The env vars a target needs to export to ``endpoint`` with ``secret``."""
    if cred.auth_mode == OTLPCredential.AuthMode.BASIC:
        return dict(otlp_env_vars(endpoint=endpoint, username=cred.username, password=secret))
    return dict(otlp_bearer_env_vars(endpoint=endpoint, token=secret))


# ─── Request authentication (cached) ────────────────────────────────────────


@dataclass(frozen=True)
class VerifiedCredential:
    """What ingestion needs from an authenticated credential, no DB row."""

    id: int
    target_id: str
    capture_level: str
    auth_mode: str


AUTH_CACHE_SECONDS = float(os.environ.get("SIMPLEAUDIT_OTLP_AUTH_CACHE_SECONDS") or 30)
_NEGATIVE_CACHE_SECONDS = 5.0
_AUTH_CACHE_MAX = 1024
_auth_cache: dict[str, tuple[float, VerifiedCredential | None]] = {}
_auth_lock = threading.Lock()


def clear_auth_cache() -> None:
    with _auth_lock:
        _auth_cache.clear()


@receiver([post_save, post_delete], sender=OTLPCredential)
def _credential_changed(**_kwargs) -> None:
    """Any credential change takes effect at once in this process (others: within the TTL)."""
    clear_auth_cache()


def _verify_header(authorization: str | None, allow_unauthenticated: bool) -> OTLPCredential | None:
    from simpleaudit.tracing.auth import parse_basic_header, parse_bearer_header

    if not authorization:
        # Targets that can't send credentials use an enabled "none" credential.
        if not allow_unauthenticated:
            return None
        return OTLPCredential.objects.filter(auth_mode=OTLPCredential.AuthMode.NONE, enabled=True).first()
    # A presented credential must verify: never fall back to the open one.
    scheme = authorization.strip().lower()
    if scheme.startswith("basic"):
        parsed = parse_basic_header(authorization)
        if not parsed:
            return None
        return verify_basic(*parsed) or _heal_open_webui_credential(*parsed)
    if scheme.startswith("bearer"):
        token = parse_bearer_header(authorization)
        return verify_bearer(token) if token else None
    return None


def _heal_open_webui_credential(username: str, password: str) -> OTLPCredential | None:
    """Register Studio's own Open WebUI credential on first use if it is missing.

    Open WebUI exports with a password Studio derives (``chat.config.otlp_password``).
    If its credential row was never registered (a start-up ordering race, a reset
    database), a request carrying exactly that password registers it and retries once,
    so the exporter never has to wait for a sync. Any other login still fails.
    """
    try:
        from chat import config as chat_config
        from chat.sync import ensure_agent_otlp_credentials

        expected = chat_config.otlp_password() if chat_config.OTLP_ENABLED else ""
        if not expected or username != chat_config.OTLP_USERNAME:
            return None
        if not hmac.compare_digest(password.encode(), expected.encode()):
            return None
        if not ensure_agent_otlp_credentials():
            return None
        return verify_basic(username, password)
    except Exception:
        logger.exception("Open WebUI OTLP credential heal failed")
        return None


def authenticate(authorization: str | None, *, allow_unauthenticated: bool = True) -> VerifiedCredential | None:
    """Authenticate an OTLP export's ``Authorization`` header.

    Results are cached by the hash of the header for ``AUTH_CACHE_SECONDS`` (bad
    headers for a few seconds), so a steady exporter costs no DB query per batch.
    """
    key = hashlib.sha256(f"{int(allow_unauthenticated)}|{authorization or ''}".encode()).hexdigest()
    now = time.monotonic()
    ttl = AUTH_CACHE_SECONDS
    if ttl > 0:
        with _auth_lock:
            hit = _auth_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    cred = _verify_header(authorization, allow_unauthenticated)
    verified = (
        VerifiedCredential(cred.id, cred.target_id, cred.capture_level, cred.auth_mode) if cred else None
    )
    if ttl > 0:
        with _auth_lock:
            if len(_auth_cache) >= _AUTH_CACHE_MAX:
                _auth_cache.clear()
            _auth_cache[key] = (now + (ttl if verified else min(ttl, _NEGATIVE_CACHE_SECONDS)), verified)
    return verified


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


# ─── Span retention (claimed vs held) ───────────────────────────────────────


def claimed_trace_ids(target_id: str, trace_ids: set[str]) -> set[str]:
    """The subset of ``trace_ids`` a run has claimed for ``target_id``."""
    if not trace_ids:
        return set()
    return set(
        OtlpTraceClaim.objects.filter(target_id=target_id, trace_id__in=trace_ids).values_list(
            "trace_id", flat=True
        )
    )


def promote_claimed_spans(target_id: str, trace_ids: set[str]) -> None:
    """Keep held spans of claimed traces (clear ``held_until``)."""
    claimed = claimed_trace_ids(target_id, trace_ids)
    if claimed:
        OtlpSpan.objects.filter(target_id=target_id, trace_id__in=claimed, held_until__isnull=False).update(
            held_until=None
        )


def claim_traces(target_id: str, trace_ids: set[str]) -> None:
    """Record that a run wants these traces: promote what arrived, keep the rest."""
    trace_ids = {t for t in trace_ids if t}
    if not trace_ids:
        return
    with transaction.atomic():
        now = timezone.now()
        OtlpTraceClaim.objects.bulk_create(
            [OtlpTraceClaim(target_id=target_id, trace_id=t, claimed_at=now) for t in trace_ids],
            ignore_conflicts=True,
        )
        promote_claimed_spans(target_id, trace_ids)


def prune_spans(now=None) -> None:
    """Drop expired holds, cap held spans per target, and drop stale kept spans.

    Kept spans of a trace are removed once its claim is older than
    ``CLAIMED_SPAN_RETENTION`` and nothing newer claims it; spans with no claim
    at all (legacy rows, traces never claimed) age out the same way.
    """
    now = now or timezone.now()
    cutoff = now - CLAIMED_SPAN_RETENTION

    OtlpSpan.objects.filter(held_until__lt=now).delete()

    held = OtlpSpan.objects.filter(held_until__isnull=False)
    for target_id in held.values_list("target_id", flat=True).distinct():
        overflow = list(
            OtlpSpan.objects.filter(target_id=target_id, held_until__isnull=False)
            .order_by("-received_at", "-id")
            .values_list("id", flat=True)[SPAN_HOLD_CAP:]
        )
        for start in range(0, len(overflow), 500):
            OtlpSpan.objects.filter(id__in=overflow[start : start + 500]).delete()

    fresh_claim = OtlpTraceClaim.objects.filter(
        target_id=OuterRef("target_id"), trace_id=OuterRef("trace_id"), claimed_at__gte=cutoff
    )
    OtlpSpan.objects.filter(
        held_until__isnull=True, received_at__lt=cutoff
    ).filter(~Exists(fresh_claim)).delete()

    OtlpTraceClaim.objects.filter(claimed_at__lt=cutoff).delete()


def maybe_prune_spans() -> None:
    """Run ``prune_spans`` at most once per ``PRUNE_INTERVAL_S`` per process."""
    global _last_prune
    if time.monotonic() - _last_prune < PRUNE_INTERVAL_S:
        return
    _last_prune = time.monotonic()
    prune_spans()
