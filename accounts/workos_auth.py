"""WorkOS Magic Auth integration (passwordless email verification).

Flow:
  1. ``send_magic_auth_code`` emails the user a one-time code.
  2. ``authenticate_magic_auth`` checks the code and returns the local Django
     user (created on first sign-in).

The WorkOS API key is server-side only; the client ID is safe to expose in
templates. Neither secret is ever stored on the User model — identity is
linked via ``workos_user_id``.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

User = get_user_model()


def _client():
    from workos import WorkOSClient

    return WorkOSClient(api_key=settings.WORKOS_API_KEY)


def send_magic_auth_code(email: str, *, ip_address: str | None = None, user_agent: str | None = None):
    """Send a 6-digit Magic Auth code to the given email via WorkOS."""
    _client().user_management.create_magic_auth(
        email=email,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def authenticate_magic_auth(code: str, email: str, *, ip_address: str | None = None, user_agent: str | None = None):
    """Verify a Magic Auth code and return the local Django user.

    Returns ``(user, created)`` where ``created`` is True when the account was
    provisioned by this sign-in.
    """
    response = _client().user_management.authenticate_with_magic_auth(
        code=code,
        email=email,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    wo_user = response.user
    if wo_user is None:
        raise ValueError("WorkOS returned no user for the magic auth code.")

    username = _derive_username(wo_user.email or wo_user.id)
    user, created = User.objects.get_or_create(
        workos_user_id=wo_user.id,
        defaults={
            "username": username,
            "email": wo_user.email or "",
            "first_name": wo_user.first_name or "",
            "last_name": wo_user.last_name or "",
            "is_staff": False,
        },
    )
    # Keep profile fields fresh on subsequent sign-ins.
    if not created:
        changed = False
        if wo_user.email and user.email != wo_user.email:
            user.email = wo_user.email
            changed = True
        if wo_user.first_name and user.first_name != wo_user.first_name:
            user.first_name = wo_user.first_name
            changed = True
        if wo_user.last_name and user.last_name != wo_user.last_name:
            user.last_name = wo_user.last_name
            changed = True
        if changed:
            user.save(update_fields=["email", "first_name", "last_name"])
    return user, created


def _derive_username(email_or_id: str) -> str:
    """Derive a unique-ish username from the WorkOS email (or user id)."""
    base = (email_or_id.split("@")[0] if "@" in email_or_id else email_or_id)[:30]
    base = "".join(c for c in base.lower() if c.isalnum() or c == "_") or "user"
    candidate = base
    n = 1
    while User.objects.filter(username=candidate).exists():
        n += 1
        candidate = f"{base[:28]}-{n}"
    return candidate
