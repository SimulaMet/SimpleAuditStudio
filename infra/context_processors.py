"""Template context processors for the server-rendered UI."""

import hashlib


def gravatar_url(request):
    """Expose the current user's Gravatar URL to templates.

    The avatar is derived from the user's email (MD5 hash), so no storage or
    upload is needed. Returns ``None`` for anonymous users or users without
    an email, letting templates fall back to initials.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.email:
        return {"gravatar_url": None}
    email = user.email.strip().lower()
    md5_hash = hashlib.md5(email.encode("utf-8")).hexdigest()
    return {"gravatar_url": f"https://www.gravatar.com/avatar/{md5_hash}?d=identicon&s=128"}


def admin_status(request):
    """Expose ``is_admin`` / ``is_superuser`` to templates for nav gating.

    Admin = superuser OR holds an ADMIN membership in any project. Computed
    once per request; anonymous users get False without hitting the DB.
    """
    from accounts.services import is_any_project_admin

    user = getattr(request, "user", None)
    return {"is_admin": is_any_project_admin(user), "is_superuser": bool(user and user.is_superuser)}


def workspaces(request):
    """Expose the user's workspaces to templates (sidebar switcher).

    Returns a list of ``{id, name, is_admin, is_current, archived}`` ordered by
    name. Content access is membership-based: every user (including
    superusers) sees the workspaces they are a member of plus the Default
    workspace. Superusers see all workspaces in the Admin page instead.
    Anonymous users get an empty list without hitting the DB.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"workspaces": []}

    from django.db.models import Q

    from accounts.models import Project, ProjectMembership
    from accounts.services import DEFAULT_PROJECT_SLUG

    projects = Project.objects.filter(
        Q(memberships__user=user) | Q(slug=DEFAULT_PROJECT_SLUG)
    ).distinct()

    roles = dict(
        ProjectMembership.objects.filter(project__in=list(projects), user=user).values_list("project_id", "role")
    )
    current_id = request.session.get("active_project_id")

    items = []
    for project in projects.order_by("name"):
        items.append(
            {
                "id": project.id,
                "name": project.name,
                "is_admin": user.is_superuser or roles.get(project.id) == ProjectMembership.Role.ADMIN,
                "is_current": project.id == current_id,
                "archived": project.archived,
            }
        )
    return {"workspaces": items}


def write_access(request):
    """``can_write``: may this user change data in the current workspace?

    Pages mark edit controls with ``data-write``; base.html hides them when
    False (the server enforces the same rule in ``write_block_reason``).
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or getattr(request, "project", None) is None:
        return {"can_write": False}
    from infra.ui import write_block_reason

    return {"can_write": write_block_reason(request) is None}


# (url name, label, icon, path prefixes that make it active, admin-only)
_NAV = (
    ("dashboard", "Dashboard", "◉", ("/", "/runs/"), False),
    ("new_experiment", "New Experiment", "＋", ("/experiments/new/",), False),
    ("experiments", "Experiments", "⊞", ("/experiments/",), False),
    ("monitors", "Monitors", "↻", ("/monitors/",), False),
    ("scenarios", "Scenarios", "▤", ("/scenarios/",), False),
    ("models", "Models", "⬡", ("/models/",), False),
    ("judges", "Judges", "⚖", ("/judges/",), False),
    ("compare", "Compare", "⇄", ("/compare/",), False),
    ("health", "Health", "⚕", ("/health/",), True),
)


def nav(request):
    """Sidebar items with one active entry (the longest matching path prefix)."""
    from django.urls import reverse

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from accounts.services import is_any_project_admin

    admin = is_any_project_admin(user)
    path = request.path
    items = [
        {"url": reverse(name), "label": label, "icon": icon, "prefixes": prefixes}
        for name, label, icon, prefixes, admin_only in _NAV
        if admin or not admin_only
    ]

    def score(item):   # "/" only matches the home page itself
        return max((len(p) for p in item["prefixes"] if (path == p if p == "/" else path.startswith(p))), default=0)

    best = max(items, key=score, default=None)
    for item in items:
        item["active"] = best is not None and item is best and score(best) > 0
    return {"nav_items": items}
