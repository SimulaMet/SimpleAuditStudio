import httpx
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Project
from accounts.services import ensure_project_access
from infra.exceptions import StableAPIError
from model_registry.models import ModelConnection


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
def ping_connection(request, conn_pk):
    """Ping a connection's /models endpoint and check all registered models against it."""
    conn = ModelConnection.objects.filter(pk=conn_pk).first()
    if not conn:
        raise StableAPIError(detail="Connection not found.", code="conn_not_found", http_status=404)
    base_url = conn.base_url.rstrip("/")
    headers = {}
    api_key = conn.api_key_direct or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = httpx.get(f"{base_url}/models", headers=headers, timeout=10)
        if resp.status_code != 200:
            return Response({"status": "error", "detail": f"HTTP {resp.status_code}"})
        data = resp.json()
        server_ids = {m.get("id") for m in data.get("data", [])}
        models = []
        found_count = 0
        for rm in conn.models.all():
            found = rm.model_id in server_ids
            if found:
                found_count += 1
            models.append({"id": rm.id, "found": found})
        return Response({"status": "up", "found": found_count, "total": len(models), "models": models})
    except Exception as e:  # noqa: BLE001 - surface any registry lookup failure to the client
        return Response({"status": "error", "detail": str(e)})
