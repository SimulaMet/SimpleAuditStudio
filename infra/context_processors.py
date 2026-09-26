"""Template context processors for the server-rendered UI."""



def admin_status(request):
    """Expose ``is_admin`` / ``is_superuser`` to templates for nav gating.

    Admin = superuser OR holds an ADMIN membership in any project. Computed
    once per request; anonymous users get False without hitting the DB.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"is_admin": False, "is_superuser": False}
    if user.is_superuser:
        return {"is_admin": True, "is_superuser": True}
    from accounts.models import ProjectMembership

    return {
        "is_admin": ProjectMembership.objects.filter(
            user=user, role=ProjectMembership.Role.ADMIN
        ).exists(),
        "is_superuser": False,
    }


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
