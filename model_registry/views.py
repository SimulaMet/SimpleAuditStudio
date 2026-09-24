import httpx

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from infra.exceptions import StableAPIError
from model_registry.models import AuditProfile, ModelConnection, ModelEndpoint, RegisteredModel
from model_registry.serializers import (
    AuditProfileCreateSerializer,
    AuditProfileSerializer,
    ModelEndpointCreateSerializer,
    ModelEndpointSerializer,
)
from model_registry.services import create_audit_profile, create_model_endpoint
from accounts.models import Project
from accounts.services import ensure_project_access


def _get_project_or_404(project_id) -> Project:
    try:
        return Project.objects.get(pk=project_id)
    except Project.DoesNotExist as exc:
        raise StableAPIError(detail="Project not found.", code="project_not_found", http_status=404) from exc


def _require_project_access(user, project: Project):
    if not ensure_project_access(user, project):
        raise StableAPIError(detail="Project access denied.", code="project_access_denied", http_status=403)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_model_endpoints(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    endpoints = ModelEndpoint.objects.filter(project=project)
    return Response(ModelEndpointSerializer(endpoints, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_model_endpoint_view(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    serializer = ModelEndpointCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    endpoint = create_model_endpoint(project=project, user=request.user, **serializer.validated_data)
    return Response(ModelEndpointSerializer(endpoint).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_audit_profiles(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    profiles = AuditProfile.objects.filter(project=project)
    return Response(AuditProfileSerializer(profiles, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_audit_profile_view(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    serializer = AuditProfileCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    profile = create_audit_profile(project=project, user=request.user, **serializer.validated_data)
    return Response(AuditProfileSerializer(profile).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ping_model(request, model_pk):
    """Check if a registered model is reachable on its connection."""
    rm = RegisteredModel.objects.select_related("connection").filter(pk=model_pk).first()
    if not rm:
        raise StableAPIError(detail="Model not found.", code="model_not_found", http_status=404)
    conn = rm.connection
    base_url = conn.base_url.rstrip("/")
    headers = {}
    api_key = conn.api_key_direct or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = httpx.get(f"{base_url}/models", headers=headers, timeout=8)
        if resp.status_code != 200:
            return Response({"status": "error", "detail": f"HTTP {resp.status_code}"})
        data = resp.json()
        ids = [m.get("id") for m in data.get("data", [])]
        found = rm.model_id in ids
        return Response({"status": "up" if found else "not_found", "model_id": rm.model_id})
    except Exception as e:
        return Response({"status": "error", "detail": str(e)})
