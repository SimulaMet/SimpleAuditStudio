from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Project
from accounts.services import ensure_project_access
from infra.exceptions import StableAPIError
from scenarios.models import Scenario, ScenarioRevision, ScenarioSet, ScenarioSetVersion
from scenarios.serializers import (
    PublishScenarioSetVersionSerializer,
    ScenarioCreateSerializer,
    ScenarioListSerializer,
    ScenarioRevisionSerializer,
    ScenarioSetCreateSerializer,
    ScenarioSetSerializer,
    ScenarioSetVersionSerializer,
    ScenarioUpdateSerializer,
)
from scenarios.services import (
    create_scenario,
    create_scenario_set,
    publish_scenario_set_version,
    update_scenario_content,
)


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
def list_scenarios(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    scenarios = Scenario.objects.filter(project=project).prefetch_related("revisions").order_by("key")
    return Response(ScenarioListSerializer(scenarios, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_scenario_view(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    serializer = ScenarioCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    scenario = create_scenario(project=project, user=request.user, **serializer.validated_data)
    return Response(ScenarioListSerializer(scenario).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_scenario(request, project_id, scenario_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    try:
        scenario = Scenario.objects.get(id=scenario_id, project=project)
    except Scenario.DoesNotExist as exc:
        raise StableAPIError(detail="Scenario not found.", code="scenario_not_found", http_status=404) from exc
    return Response(ScenarioListSerializer(scenario).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_scenario_view(request, project_id, scenario_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    try:
        scenario = Scenario.objects.get(id=scenario_id, project=project)
    except Scenario.DoesNotExist as exc:
        raise StableAPIError(detail="Scenario not found.", code="scenario_not_found", http_status=404) from exc
    serializer = ScenarioUpdateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    revision = update_scenario_content(scenario=scenario, user=request.user, **serializer.validated_data)
    return Response(ScenarioRevisionSerializer(revision).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_revisions(request, project_id, scenario_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    try:
        scenario = Scenario.objects.get(id=scenario_id, project=project)
    except Scenario.DoesNotExist as exc:
        raise StableAPIError(detail="Scenario not found.", code="scenario_not_found", http_status=404) from exc
    revisions = ScenarioRevision.objects.filter(scenario=scenario).order_by("-revision")
    return Response(ScenarioRevisionSerializer(revisions, many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_scenario_sets(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    sets = ScenarioSet.objects.filter(project=project)
    return Response(ScenarioSetSerializer(sets, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_scenario_set_view(request, project_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    serializer = ScenarioSetCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    scenario_set = create_scenario_set(project=project, user=request.user, **serializer.validated_data)
    return Response(ScenarioSetSerializer(scenario_set).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_versions(request, project_id, set_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    try:
        scenario_set = ScenarioSet.objects.get(id=set_id, project=project)
    except ScenarioSet.DoesNotExist as exc:
        raise StableAPIError(detail="Scenario set not found.", code="scenario_set_not_found", http_status=404) from exc
    versions = ScenarioSetVersion.objects.filter(scenario_set=scenario_set).prefetch_related("items__scenario", "items__revision").order_by("-version")
    return Response(ScenarioSetVersionSerializer(versions, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def publish_version(request, project_id, set_id):
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    try:
        scenario_set = ScenarioSet.objects.get(id=set_id, project=project)
    except ScenarioSet.DoesNotExist as exc:
        raise StableAPIError(detail="Scenario set not found.", code="scenario_set_not_found", http_status=404) from exc
    serializer = PublishScenarioSetVersionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    version = publish_scenario_set_version(scenario_set=scenario_set, user=request.user, scenario_ids=serializer.validated_data["scenario_ids"])
    return Response(ScenarioSetVersionSerializer(version).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def export_scenarios(request, project_id):
    """Export all scenarios in a project as a portable JSON document.

    The export format is a list of scenario dicts with their latest revision
    content. This can be imported into any other SimpleAudit platform instance.
    """
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    scenarios = Scenario.objects.filter(project=project).prefetch_related("revisions").order_by("key")
    data = []
    for s in scenarios:
        rev = s.revisions.order_by("-revision").first()
        if not rev:
            continue
        data.append({
            "key": s.key,
            "title": s.title,
            "description": rev.description,
            "expected_behavior": rev.expected_behavior,
            "test_prompt": rev.test_prompt,
            "tags": s.tags or [],
        })
    response = Response({"scenarios": data, "count": len(data), "exported_at": __import__("django.utils.timezone", fromlist=["timezone"]).now().isoformat()})
    response["Content-Disposition"] = 'attachment; filename="scenarios-export.json"'
    return response


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def import_scenarios(request, project_id):
    """Import scenarios from a JSON export document.

    Accepts the format produced by export_scenarios: {"scenarios": [...]} or a
    bare list. Scenarios with existing keys are skipped (not overwritten);
    new ones are created. Returns a summary of created/skipped counts.
    """
    project = _get_project_or_404(project_id)
    _require_project_access(request.user, project)
    payload = request.data
    items = payload.get("scenarios", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise StableAPIError(detail="Expected a list of scenarios or {\"scenarios\": [...]}.", code="invalid_import_format", http_status=400)

    created = 0
    skipped = 0
    errors = []
    for item in items:
        key = item.get("key", "").strip()
        if not key:
            errors.append({"item": item, "error": "missing key"})
            continue
        if Scenario.objects.filter(project=project, key=key).exists():
            skipped += 1
            continue
        try:
            create_scenario(
                project=project,
                user=request.user,
                key=key,
                title=item.get("title", key),
                description=item.get("description", ""),
                expected_behavior=item.get("expected_behavior", []),
                test_prompt=item.get("test_prompt", ""),
                tags=item.get("tags", []),
            )
            created += 1
        except Exception as exc:  # noqa: BLE001 - collect per-scenario import errors
            errors.append({"key": key, "error": str(exc)})

    return Response({"created": created, "skipped": skipped, "errors": errors}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
