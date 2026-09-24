"""Template context processors for the server-rendered UI."""


def admin_status(request):
    """Expose ``is_admin`` to templates so nav can show admin-only entries.

    Admin = superuser OR holds an ADMIN membership in any project. Computed
    once per request; anonymous users get False without hitting the DB.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"is_admin": False}
    if user.is_superuser:
        return {"is_admin": True}
    from accounts.models import ProjectMembership

    return {
        "is_admin": ProjectMembership.objects.filter(
            user=user, role=ProjectMembership.Role.ADMIN
        ).exists()
    }
