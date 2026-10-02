from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from infra.exceptions import StableAPIError
from model_registry.models import ModelConnection
from model_registry.services import fetch_remote_model_ids, http_error_detail


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ping_connection(request, conn_pk):
    """Ping a connection's /models endpoint and check all registered models against it.

    Allowed for the owning workspace and for any workspace the connection is
    shared into (public / admin-shared / explicit). Pinging only reads the
    remote model list — it never exposes the API key.
    """
    from model_registry.services import visible_connection_ids_for

    conn = ModelConnection.objects.select_related("project").filter(pk=conn_pk).first()
    if conn is None:
        raise StableAPIError(detail="Connection not found.", code="conn_not_found", http_status=404)
    # The caller must be working in a workspace that can see this connection.
    # Fall back to the connection's own project when no active project is set
    # (e.g. API token auth without a session).
    active_project = getattr(request, "project", None) or conn.project
    if conn.id not in visible_connection_ids_for(active_project):
        raise StableAPIError(detail="Connection not found.", code="conn_not_found", http_status=404)
    try:
        server_ids = set(fetch_remote_model_ids(conn))
    except Exception as e:  # noqa: BLE001 - surface any server failure to the client
        return Response({"status": "error", "detail": http_error_detail(e)})
    models = [{"id": rm.id, "found": rm.model_id in server_ids} for rm in conn.models.all()]
    found = sum(m["found"] for m in models)
    return Response({"status": "up", "found": found, "total": len(models), "models": models})
