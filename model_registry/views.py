from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.services import ensure_project_access
from infra.exceptions import StableAPIError
from model_registry.models import ModelConnection
from model_registry.services import fetch_remote_model_ids, http_error_detail


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ping_connection(request, conn_pk):
    """Ping a connection's /models endpoint and check all registered models against it."""
    conn = ModelConnection.objects.select_related("project").filter(pk=conn_pk).first()
    if not conn or not ensure_project_access(request.user, conn.project):
        raise StableAPIError(detail="Connection not found.", code="conn_not_found", http_status=404)
    try:
        server_ids = set(fetch_remote_model_ids(conn))
    except Exception as e:  # noqa: BLE001 - surface any server failure to the client
        return Response({"status": "error", "detail": http_error_detail(e)})
    models = [{"id": rm.id, "found": rm.model_id in server_ids} for rm in conn.models.all()]
    found = sum(m["found"] for m in models)
    return Response({"status": "up", "found": found, "total": len(models), "models": models})
