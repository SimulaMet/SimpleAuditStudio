"""WorkOS AuthKit integration (passwordless email verification + SSO).

Flow:
  1. ``build_authorization_url`` sends the browser to WorkOS's hosted login UI.
  2. WorkOS redirects back to ``/auth/workos/callback/`` with a one-time code.
  3. ``exchange_code_for_user`` trades the code for an authenticated user and
     returns the local Django user (created on first sign-in).

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


def build_authorization_url(redirect_uri: str, state: str, login_hint: str | None = None) -> str:
    """Build the URL that starts the WorkOS AuthKit flow.

    ``login_hint`` pre-fills the user's email and routes to Magic Auth
    (email code) instead of auto-redirecting to an SSO/OAuth connection.
    """
    kwargs: dict = {
        "client_id": settings.WORKOS_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if login_hint:
        kwargs["login_hint"] = login_hint
    return _client().user_management.get_authorization_url(**kwargs)


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


def exchange_code_for_user(code: str, *, ip_address: str | None = None, user_agent: str | None = None):
    """Exchange an OAuth callback code for a local Django user (SSO/OAuth flow).

    Kept for future SSO support; the primary flow uses Magic Auth directly.
    """
    response = _client().user_management.authenticate_with_code(
        code=code,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    wo_user = response.user
    if wo_user is None:
        raise ValueError("WorkOS returned no user for the authentication code.")

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
