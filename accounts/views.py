"""Foundation views: health, auth, and project management."""
import logging

from django.contrib.auth import authenticate, login
from django.http import JsonResponse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from accounts.models import Project, ProjectMembership, User
from accounts.serializers import (
    MemberAddSerializer,
    MemberRoleSerializer,
    ProjectMembershipSerializer,
    RegisterSerializer,
    UserSerializer,
    WorkspaceCreateSerializer,
    WorkspaceItemSerializer,
    WorkspaceUpdateSerializer,
)
from accounts.services import (
    DEFAULT_PROJECT_SLUG,
    create_workspace,
    delete_workspace,
    ensure_project_access,
    update_workspace,
)
from infra.exceptions import StableAPIError
from infra.readiness import ready_payload

logger = logging.getLogger(__name__)


def healthz(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    payload = ready_payload()
    status_code = 200 if payload["status"] == "ready" else 503
    return JsonResponse(payload, status=status_code)


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def obtain_token(request):
    """Issue a DRF auth token for username/password.

    The SPA uses token auth (``Authorization: Token <key>``) so it works across
    origins and without CSRF handling. Returns the token plus the user payload.
    """
    username = request.data.get("username", "")
    password = request.data.get("password", "")
    user = authenticate(request, username=username, password=password)
    if user is None:
        raise StableAPIError(detail="Invalid credentials.", code="invalid_credentials", http_status=401)
    token, _created = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": UserSerializer(user).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UserSerializer(request.user).data)


# ─── Workspaces (a.k.a. Projects) ─────────────────────────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_workspaces(request):
    if request.user.is_superuser:
        projects = Project.objects.all()
    else:
        from django.db.models import Q

        projects = Project.objects.filter(
            Q(memberships__user=request.user) | Q(slug=DEFAULT_PROJECT_SLUG)
        ).distinct()
    return Response(_serialize_workspaces(projects, request))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_workspace_view(request):
    serializer = WorkspaceCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    project = create_workspace(user=request.user, **serializer.validated_data)
    return Response(_serialize_workspaces([project], request)[0], status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def workspace_detail(request, project_id):
    project = _get_project_or_404(project_id)
    if not ensure_project_access(request.user, project):
        raise StableAPIError(detail="Workspace access denied.", code="workspace_access_denied", http_status=403)

    if request.method == "DELETE":
        delete_workspace(user=request.user, project=project)
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method in ("PATCH", "PUT"):
        serializer = WorkspaceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = update_workspace(user=request.user, project=project, **serializer.validated_data)

    return Response(_serialize_workspaces([project], request)[0])


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def switch_workspace(request):
    """Set the session's active workspace so all UI pages scope to it."""
    raw_id = request.data.get("project_id") or request.data.get("workspace_id")
    if not raw_id or not str(raw_id).isdigit():
        raise StableAPIError(detail="project_id is required.", code="invalid_workspace_id")
    project = _get_project_or_404(int(raw_id))
    if not ensure_project_access(request.user, project):
        raise StableAPIError(detail="You are not a member of this workspace.", code="workspace_access_denied", http_status=403)
    request.session["active_project_id"] = project.id
    return Response({"ok": True, "workspace": _serialize_workspaces([project], request)[0]})


def _serialize_workspaces(projects, request) -> list[dict]:
    items = WorkspaceItemSerializer(projects, many=True, context={"request": request}).data
    # Batch the per-workspace admin check into one query for non-superusers.
    if request.user.is_superuser:
        for item in items:
            item["is_admin"] = True
        return items
    roles = dict(
        ProjectMembership.objects.filter(
            project__in=list(projects), user=request.user
        ).values_list("project_id", "role")
    )
    for item in items:
        item["is_admin"] = roles.get(item["id"]) == ProjectMembership.Role.ADMIN
    return items


# ─── Team members ────────────────────────────────────────────────────────────


def _require_workspace_admin_request(request, project) -> None:
    if request.user.is_superuser:
        return
    if not ProjectMembership.objects.filter(
        project=project, user=request.user, role=ProjectMembership.Role.ADMIN
    ).exists():
        raise StableAPIError(detail="Workspace admin role required.", code="workspace_admin_required", http_status=403)


def _admin_count(project) -> int:
    return project.memberships.filter(role=ProjectMembership.Role.ADMIN).count()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_members(request, project_id):
    project = _get_project_or_404(project_id)
    if not ensure_project_access(request.user, project):
        raise StableAPIError(detail="Workspace access denied.", code="workspace_access_denied", http_status=403)
    memberships = project.memberships.select_related("user").order_by("created_at")
    return Response(ProjectMembershipSerializer(memberships, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def add_member(request, project_id):
    project = _get_project_or_404(project_id)
    _require_workspace_admin_request(request, project)

    serializer = MemberAddSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    username = serializer.validated_data["username"]
    role = serializer.validated_data["role"]
    try:
        target = User.objects.get(username=username)
    except User.DoesNotExist:
        raise StableAPIError(detail="User not found.", code="user_not_found", http_status=404)

    membership, _created = ProjectMembership.objects.update_or_create(
        project=project,
        user=target,
        defaults={"role": role},
    )
    return Response(ProjectMembershipSerializer(membership).data, status=status.HTTP_201_CREATED)


@api_view(["PUT", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def update_or_remove_member(request, project_id, user_id):
    project = _get_project_or_404(project_id)
    _require_workspace_admin_request(request, project)

    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        raise StableAPIError(detail="User not found.", code="user_not_found", http_status=404)
    if target.is_superuser:
        raise StableAPIError(detail="System (superuser) accounts cannot be modified.", code="cannot_modify_superuser", http_status=403)

    try:
        membership = project.memberships.get(user=target)
    except ProjectMembership.DoesNotExist:
        raise StableAPIError(detail="User is not a member of this workspace.", code="member_not_found", http_status=404)

    if request.method == "DELETE":
        if membership.role == ProjectMembership.Role.ADMIN and _admin_count(project) <= 1:
            raise StableAPIError(detail="A workspace must keep at least one admin.", code="last_admin", http_status=409)
        membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = MemberRoleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    new_role = serializer.validated_data["role"]
    if membership.role == ProjectMembership.Role.ADMIN and new_role != ProjectMembership.Role.ADMIN and _admin_count(project) <= 1:
        raise StableAPIError(detail="A workspace must keep at least one admin.", code="last_admin", http_status=409)
    membership.role = new_role
    membership.save(update_fields=["role"])
    return Response(ProjectMembershipSerializer(membership).data)


# ─── Legacy aliases (kept for backward compatibility) ────────────────────────


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_projects(request):
    return list_workspaces(request)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_project(request):
    return create_workspace_view(request)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_project(request, project_id):
    return workspace_detail(request, project_id)


def _get_project_or_404(project_id) -> Project:
    try:
        return Project.objects.get(pk=project_id)
    except Project.DoesNotExist as exc:
        raise StableAPIError(detail="Workspace not found.", code="workspace_not_found", http_status=404) from exc
