"""Template context processors for the server-rendered UI."""

import hashlib

from django.conf import settings


def gravatar_for(user) -> str | None:
    """The user's Gravatar URL, or ``None`` so templates fall back to initials.

    The URL carries the MD5 hash of the user's email, and the browser sends it
    to gravatar.com when it loads the image. Off unless ``SIMPLEAUDIT_GRAVATAR``
    is set. Also ``None`` for anonymous users and users without an email.
    """
    if not settings.SIMPLEAUDIT_GRAVATAR:
        return None
    if user is None or not user.is_authenticated or not user.email:
        return None
    email = user.email.strip().lower()
    md5_hash = hashlib.md5(email.encode("utf-8")).hexdigest()
    return f"https://www.gravatar.com/avatar/{md5_hash}?d=identicon&s=128"


def gravatar_url(request):
    """Expose the current user's Gravatar URL (or ``None``) and the setting."""
    return {
        "gravatar_url": gravatar_for(getattr(request, "user", None)),
        "gravatar_enabled": settings.SIMPLEAUDIT_GRAVATAR,
    }


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


# (url name, label, icon, path prefixes that make it active, admin-only, children)
# children is a tuple of (url name, label, icon) rendered as indented sub-items.
_NAV = (
    ("dashboard", "Dashboard", "◉", ("/", "/runs/"), False, ()),
    ("new_experiment", "New Experiment", "＋", ("/experiments/new/",), False, ()),
    ("experiments", "Experiments", "⊞", ("/experiments/",), False, ()),
    ("monitors", "Monitors", "↻", ("/monitors/",), False, ()),
    ("scenarios", "Scenarios", "▤", ("/scenarios/",), False, ()),
    # The Agents surface is chat-backed (agents sync to Open WebUI; knowledge /
    # tools live in the Open WebUI workspace); it is skipped in nav() when the
    # chat module is off.
    ("agents", "Agents", "🤖", ("/agents/",), False, (
        ("agents_knowledge", "Knowledge Bases", "📚"),
        ("agents_tools", "Tools", "🔧"),
    )),
    ("connections", "Connections", "⬡", ("/connections/",), False, ()),
    ("judges", "Judges", "⚖", ("/judges/",), False, ()),
    ("prompts", "System Prompts", "¶", ("/prompts/",), False, ()),
    ("compare", "Compare", "⇄", ("/compare/",), False, ()),
    ("visualizer", "Visualizer", "◫", ("/visualizer/",), False, ()),
    ("health", "Health", "⚕", ("/health/",), True, ()),
)


def nav(request):
    """Sidebar items with one active entry (the longest matching path prefix)."""
    from django.urls import reverse

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from accounts.services import is_any_project_admin
    from infra.chat_feature import chat_enabled

    admin = is_any_project_admin(user)
    path = request.path
    chat_on = chat_enabled()
    # The wrapper page (model-picker bar + iframe of the embedded service at
    # /chat/) lives at /playground/.
    entries = _NAV + ((("chat", "Playground", "💬", ("/playground/",), False, ()),) if chat_on else ())
    items = [
        {
            "url": reverse(name),
            "label": label,
            "icon": icon,
            "prefixes": prefixes,
            "children": [
                {"url": reverse(cname), "label": clabel, "icon": cicon}
                for cname, clabel, cicon in children
            ],
        }
        for name, label, icon, prefixes, admin_only, children in entries
        if (admin or not admin_only)
        # The Agents surface (and its Knowledge Bases / Tools children) is
        # chat-backed; keep it out of the nav while chat is off.
        and (name != "agents" or chat_on)
    ]

    def score(item):   # "/" only matches the home page itself
        return max((len(p) for p in item["prefixes"] if (path == p if p == "/" else path.startswith(p))), default=0)

    best = max(items, key=score, default=None)
    for item in items:
        # Children are top-level pages with their own URLs; mark each active
        # when its URL matches exactly (e.g. /agents/knowledge/).
        for child in item["children"]:
            child["active"] = child["url"] == path
        # For sub-pages (children), expose the exact child label so the
        # breadcrumb can render "Agents / Knowledge" instead of just "Agents".
        item["active_child"] = next(
            (c["label"] for c in item["children"] if c["url"] == path), None
        )
        # Parent is active only when no child claims the current page —
        # otherwise both would be highlighted simultaneously.
        has_active_child = any(c["active"] for c in item["children"])
        item["active"] = (
            best is not None and item is best and score(best) > 0
            and not has_active_child
        )
    return {"nav_items": items}
