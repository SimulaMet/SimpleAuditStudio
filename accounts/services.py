"""Business logic for foundation operations.

Views should call these services rather than containing authorization or
bootstrap rules directly.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from accounts.models import Project, ProjectMembership
from infra.exceptions import StableAPIError

User = get_user_model()


@transaction.atomic
def bootstrap_admin_and_default_project(
    *,
    username: str,
    email: str,
    password: str,
    project_name: str = "Default",
) -> tuple[User, Project]:
    """Idempotently create the initial admin user and default project."""
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"email": email, "is_staff": True, "is_superuser": True},
    )
    # Always ensure the admin password matches the configured value. This makes
    # the bootstrap idempotent across container restarts where the Postgres data
    # volume persists (e.g. HF Spaces with non-ephemeral storage). Without this,
    # a pre-existing admin user created with a different password would never be
    # corrected, breaking login on subsequent starts.
    user.set_password(password)
    user.save(update_fields=["password"])
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])
    if not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    if not user.is_superuser:
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])

    slug = slugify(project_name) or "default"
    project, _ = Project.objects.get_or_create(slug=slug, defaults={"name": project_name})
    ProjectMembership.objects.get_or_create(
        project=project,
        user=user,
        defaults={"role": ProjectMembership.Role.ADMIN},
    )
    return user, project


def ensure_project_access(user, project) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return ProjectMembership.objects.filter(project=project, user=user).exists()


def _require_workspace_admin(user, project) -> None:
    """Raise 403 unless the user is a superuser or ADMIN member of THIS workspace."""
    if not user or not user.is_authenticated:
        raise StableAPIError(detail="Authentication required.", code="authentication_required", http_status=401)
    if user.is_superuser:
        return
    if not ProjectMembership.objects.filter(
        project=project, user=user, role=ProjectMembership.Role.ADMIN
    ).exists():
        raise StableAPIError(detail="Workspace admin role required.", code="workspace_admin_required", http_status=403)


@transaction.atomic
def create_workspace(*, user, name: str, description: str = "") -> Project:
    """Create a workspace; the creator becomes its ADMIN member.

    Allowed for superusers and any user who already administers at least one
    workspace (so teams can grow without contacting the platform operator).
    """
    if not user or not user.is_authenticated:
        raise StableAPIError(detail="Authentication required.", code="authentication_required", http_status=401)
    if not user.is_superuser and not ProjectMembership.objects.filter(
        user=user, role=ProjectMembership.Role.ADMIN
    ).exists():
        raise StableAPIError(detail="You must administer a workspace before creating new ones.", code="workspace_create_forbidden", http_status=403)

    clean_name = (name or "").strip()
    if not clean_name:
        raise StableAPIError(detail="Workspace name is required.", code="invalid_workspace_name")

    base_slug = slugify(clean_name) or "workspace"
    for attempt in range(5):
        candidate = base_slug if attempt == 0 else f"{base_slug}-{attempt + 1}"
        try:
            with transaction.atomic():
                project = Project.objects.create(name=clean_name, slug=candidate, description=(description or "").strip())
                ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
            return project
        except IntegrityError:
            if attempt == 4:
                raise StableAPIError(detail="A workspace with this name already exists.", code="workspace_name_conflict", http_status=409)
            continue
    raise StableAPIError(detail="A workspace with this name already exists.", code="workspace_name_conflict", http_status=409)


@transaction.atomic
def update_workspace(*, user, project: Project, name: str | None = None, description: str | None = None) -> Project:
    """Rename or re-describe a workspace. The slug is immutable — it is part
    of frozen audit provenance and must never change after creation."""
    _require_workspace_admin(user, project)
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise StableAPIError(detail="Workspace name cannot be empty.", code="invalid_workspace_name")
        project.name = clean_name
    if description is not None:
        project.description = description.strip()
    try:
        project.save()
    except IntegrityError as exc:
        raise StableAPIError(detail="A workspace with this name already exists.", code="workspace_name_conflict", http_status=409) from exc
    return project


def delete_workspace(*, user, project: Project) -> None:
    """Delete an EMPTY workspace. Refuses while any audit content exists so a
    mistaken click can never destroy experiment history."""
    _require_workspace_admin(user, project)

    from audits.models import AuditRun
    from model_registry.models import ModelConnection, ModelEndpoint
    from scenarios.models import Scenario, ScenarioSet

    blockers = {
        "scenarios": Scenario.objects.filter(project=project).exists(),
        "scenario sets": ScenarioSet.objects.filter(project=project).exists(),
        "model endpoints": ModelEndpoint.objects.filter(project=project).exists(),
        "model connections": ModelConnection.objects.filter(project=project).exists(),
        "audit runs": AuditRun.objects.filter(project=project).exists(),
    }
    present = [label for label, found in blockers.items() if found]
    if present:
        raise StableAPIError(
            detail=f"This workspace still contains: {', '.join(present)}. Remove them first.",
            code="workspace_not_empty",
            http_status=409,
        )
    project.delete()
