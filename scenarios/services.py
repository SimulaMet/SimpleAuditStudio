"""Services for scenario creation, revisioning, and immutable set publishing."""
from django.db import transaction
from django.utils.text import slugify

from accounts.models import Project, ProjectMembership
from accounts.services import ensure_project_access, require_project_writable
from infra.exceptions import StableAPIError
from infra.hashing import scenario_revision_hash, scenario_set_version_hash
from scenarios.models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


def require_project_role(user, project: Project, *roles):
    if not user or not user.is_authenticated:
        raise StableAPIError(detail="Authentication required.", code="authentication_required", http_status=401)
    if user.is_superuser:
        return
    # Archived workspaces are read-only for non-superusers.
    require_project_writable(user, project)
    if not ensure_project_access(user, project):
        raise StableAPIError(detail="Project access denied.", code="project_access_denied", http_status=403)
    if roles and not ProjectMembership.objects.filter(project=project, user=user, role__in=list(roles)).exists():
        raise StableAPIError(detail="Insufficient project role.", code="insufficient_role", http_status=403)


@transaction.atomic
def create_scenario(*, project: Project, user, key: str, title: str, description: str, expected_behavior, test_prompt: str = "", metadata: dict | None = None, category: str = "", tags: list | None = None) -> Scenario:
    require_project_role(user, project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    normalized_key = slugify(key) or slugify(title)
    if not normalized_key:
        raise StableAPIError(detail="Scenario key or title is required.", code="invalid_scenario_key")

    scenario = Scenario.objects.create(
        project=project,
        key=normalized_key,
        title=title.strip(),
        category=category.strip(),
        tags=tags or [],
        created_by=user,
    )
    _create_revision(scenario=scenario, user=user, description=description, expected_behavior=expected_behavior, test_prompt=test_prompt, metadata=metadata or {})
    return scenario


@transaction.atomic
def update_scenario_content(*, scenario: Scenario, user, description: str, expected_behavior, test_prompt: str = "", metadata: dict | None = None) -> ScenarioRevision:
    require_project_role(user, scenario.project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    latest = ScenarioRevision.objects.filter(scenario=scenario).order_by("-revision").first()
    new_metadata = metadata if metadata is not None else (latest.metadata if latest else {})
    if (
        latest
        and latest.description == description
        and latest.expected_behavior == expected_behavior
        and latest.test_prompt == test_prompt
        and latest.metadata == new_metadata
    ):
        return latest
    return _create_revision(scenario=scenario, user=user, description=description, expected_behavior=expected_behavior, test_prompt=test_prompt, metadata=new_metadata)


def _create_revision(*, scenario: Scenario, user, description: str, expected_behavior, test_prompt: str, metadata: dict) -> ScenarioRevision:
    next_revision = ScenarioRevision.objects.filter(scenario=scenario).count() + 1
    content_hash = scenario_revision_hash(
        description=description,
        expected_behavior=expected_behavior,
        test_prompt=test_prompt,
        metadata=metadata,
    )
    return ScenarioRevision.objects.create(
        scenario=scenario,
        revision=next_revision,
        description=description,
        expected_behavior=expected_behavior,
        test_prompt=test_prompt,
        metadata=metadata,
        content_hash=content_hash,
        created_by=user,
    )


@transaction.atomic
def create_scenario_set(*, project: Project, user, name: str, description: str = "") -> ScenarioSet:
    require_project_role(user, project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    return ScenarioSet.objects.create(project=project, name=name.strip(), description=description.strip(), created_by=user)


@transaction.atomic
def publish_scenario_set_version(*, scenario_set: ScenarioSet, user, scenario_ids: list[int]) -> ScenarioSetVersion:
    """Publish an immutable ordered version from current latest revisions.

    Concurrency note: two concurrent publishes may attempt the same next version.
    The unique `(scenario_set, version)` constraint rejects one; callers should
    retry after re-reading the latest version.
    """
    require_project_role(user, scenario_set.project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    if not scenario_ids:
        raise StableAPIError(detail="At least one scenario is required.", code="empty_scenario_set")

    scenarios = {scenario.id: scenario for scenario in Scenario.objects.filter(id__in=scenario_ids, project=scenario_set.project)}
    if len(scenarios) != len(set(scenario_ids)):
        missing = sorted(set(scenario_ids) - set(scenarios))
        raise StableAPIError(detail=f"Scenarios not found in project: {missing}", code="scenario_not_found")

    items = []
    for position, scenario_id in enumerate(scenario_ids, start=1):
        scenario = scenarios[scenario_id]
        revision = ScenarioRevision.objects.filter(scenario=scenario).order_by("-revision").first()
        if not revision:
            raise StableAPIError(detail=f"Scenario {scenario.key} has no revision.", code="scenario_missing_revision")
        items.append(
            {
                "position": position,
                "scenario": scenario,
                "revision": revision,
            }
        )

    next_version = ScenarioSetVersion.objects.filter(scenario_set=scenario_set).count() + 1
    hash_items = [
        {
            "position": item["position"],
            "scenario_id": item["scenario"].id,
            "scenario_key": item["scenario"].key,
            "revision": item["revision"].revision,
            "revision_content_hash": item["revision"].content_hash,
        }
        for item in items
    ]
    content_hash = scenario_set_version_hash(hash_items)

    version = ScenarioSetVersion.objects.create(
        scenario_set=scenario_set,
        version=next_version,
        scenario_count=len(items),
        content_hash=content_hash,
        published_by=user,
    )
    for item in items:
        ScenarioSetVersionItem.objects.create(
            version=version,
            scenario=item["scenario"],
            revision=item["revision"],
            position=item["position"],
        )
    return version
