"""Admin-gated health API endpoint for the Health panel."""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import ProjectMembership
from infra.health import collect_health


def _is_admin(user) -> bool:
    """System health is whole-system, so gate on superuser OR any ADMIN role."""
    if user.is_superuser:
        return True
    return ProjectMembership.objects.filter(
        user=user, role=ProjectMembership.Role.ADMIN
    ).exists()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def health_panel_api(request):
    """Return the full system health snapshot (admin-only)."""
    if not _is_admin(request.user):
        return Response(
            {"detail": "Admin access required.", "code": "insufficient_role"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return Response(collect_health())
