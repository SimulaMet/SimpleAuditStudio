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


def has_local_password(user) -> bool:
    """True when the user has a usable local password hash.

    WorkOS-provisioned accounts are created with an empty password, so they
    cannot authenticate via username + password until one is set. Django's
    ``has_usable_password()`` returns True for those (empty string is treated
    as "usable"), so we check the hash directly.
    """
    return bool(user.password) and user.has_usable_password()


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
    default_description = "Public common workspaces visible to all users, used for demo"
    project, created = Project.objects.get_or_create(
        slug=slug,
        defaults={"name": project_name, "description": default_description},
    )
    if not created and not project.description:
        project.description = default_description
        project.save(update_fields=["description"])
    ProjectMembership.objects.get_or_create(
        project=project,
        user=user,
        defaults={"role": ProjectMembership.Role.ADMIN},
    )
    return user, project


#: Slug of the shared workspace visible to every authenticated user.
DEFAULT_PROJECT_SLUG = "default"


def ensure_project_access(user, project) -> bool:
    """Return True if the user may view this project's content.

    Content access is membership-based. The Default workspace is visible to
    every authenticated user. A superuser is NOT automatically granted access
    to every project — they must be a member (or rely on the Default
    workspace). Superusers keep full management rights via the Admin page.
    """
    if not user or not user.is_authenticated:
        return False
    # The Default workspace is always accessible to all users.
    if project.slug == DEFAULT_PROJECT_SLUG:
        return True
    return ProjectMembership.objects.filter(project=project, user=user).exists()


def require_project_writable(user, project) -> None:
    """Raise 403 when mutating an archived workspace as a non-superuser.

    Archived workspaces are read-only for their members. Superusers bypass the
    check so they can still manage an archived workspace from the Admin page.
    """
    if not user or not user.is_authenticated:
        raise StableAPIError(detail="Authentication required.", code="authentication_required", http_status=401)
    if project.archived and not user.is_superuser:
        raise StableAPIError(
            detail="This workspace is archived and read-only.",
            code="workspace_archived",
            http_status=403,
        )


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

    Any authenticated user may create a workspace — this is how new users
    bootstrap their own space without needing an existing admin to invite them.
    """
    if not user or not user.is_authenticated:
        raise StableAPIError(detail="Authentication required.", code="authentication_required", http_status=401)

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
    require_project_writable(user, project)
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


def workspace_has_content(project: Project) -> bool:
    """True if the workspace still holds any scenarios, models, or audits."""
    from audits.models import AuditRun
    from model_registry.models import ModelConnection
    from scenarios.models import Scenario, ScenarioSet

    return (
        Scenario.objects.filter(project=project).exists()
        or ScenarioSet.objects.filter(project=project).exists()
        or ModelConnection.objects.filter(project=project).exists()
        or AuditRun.objects.filter(project=project).exists()
    )


def delete_workspace(*, user, project: Project) -> None:
    """Delete an EMPTY workspace. Refuses while any audit content exists so a
    mistaken click can never destroy experiment history."""
    _require_workspace_admin(user, project)

    from audits.models import AuditRun
    from model_registry.models import ModelConnection
    from scenarios.models import Scenario, ScenarioSet

    blockers = {
        "scenarios": Scenario.objects.filter(project=project).exists(),
        "scenario sets": ScenarioSet.objects.filter(project=project).exists(),
        "model connections": ModelConnection.objects.filter(project=project).exists(),
        "audit runs": AuditRun.objects.filter(project=project).exists(),
    }
    present = [label for label, found in blockers.items() if found]
    if present:
        raise StableAPIError(
            detail=f"This workspace still contains: {', '.join(present)}. Archive it instead.",
            code="workspace_not_empty",
            http_status=409,
        )
    project.delete()


@transaction.atomic
def archive_workspace(*, user, project: Project) -> Project:
    """Soft-hide a workspace that still contains data.

    Members keep read access; all mutations are blocked for non-superusers.
    Never deletes data.
    """
    _require_workspace_admin(user, project)
    project.archived = True
    project.save(update_fields=["archived", "updated_at"])
    return project


@transaction.atomic
def unarchive_workspace(*, user, project: Project) -> Project:
    """Restore an archived workspace to active (writable) status."""
    _require_workspace_admin(user, project)
    project.archived = False
    project.save(update_fields=["archived", "updated_at"])
    return project


# ─── Super-admin user management ─────────────────────────────────────────────


def _require_superuser(user) -> None:
    if not user or not user.is_authenticated or not user.is_superuser:
        raise StableAPIError(detail="Super admin required.", code="super_admin_required", http_status=403)


def _superuser_count() -> int:
    return User.objects.filter(is_superuser=True).count()


@transaction.atomic
def admin_create_user(*, admin_user, username: str, email: str = "", password: str, first_name: str = "", last_name: str = "") -> User:
    """Create a local account. Super admin only."""
    _require_superuser(admin_user)
    clean_username = (username or "").strip()
    if not clean_username:
        raise StableAPIError(detail="Username is required.", code="invalid_username")
    if User.objects.filter(username__iexact=clean_username).exists():
        raise StableAPIError(detail="Username already exists.", code="username_taken", http_status=409)
    clean_email = (email or "").strip()
    if clean_email and User.objects.filter(email__iexact=clean_email).exists():
        raise StableAPIError(detail="Email already exists.", code="email_taken", http_status=409)
    from django.contrib.auth.password_validation import validate_password

    validate_password(password)
    return User.objects.create_user(
        username=clean_username,
        email=clean_email,
        password=password,
        first_name=(first_name or "").strip(),
        last_name=(last_name or "").strip(),
    )


@transaction.atomic
def admin_update_user(
    *,
    admin_user,
    target: User,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    is_active: bool | None = None,
    is_superuser: bool | None = None,
    password: str | None = None,
) -> User:
    """Edit a user's profile, status, super-admin flag, or password.

    Guards: cannot demote self, cannot remove the last super admin, cannot
    deactivate the last super admin.
    """
    _require_superuser(admin_user)
    if is_superuser is False and target.is_superuser:
        if target.id == admin_user.id:
            raise StableAPIError(detail="You cannot demote yourself.", code="cannot_demote_self", http_status=409)
        if _superuser_count() <= 1:
            raise StableAPIError(detail="At least one super admin must remain.", code="last_superuser", http_status=409)
    if is_active is False and target.is_superuser and _superuser_count() <= 1:
        raise StableAPIError(detail="Cannot deactivate the last super admin.", code="last_superuser", http_status=409)

    if first_name is not None:
        target.first_name = first_name.strip()
    if last_name is not None:
        target.last_name = last_name.strip()
    if email is not None:
        clean_email = email.strip()
        if clean_email and User.objects.filter(email__iexact=clean_email).exclude(pk=target.pk).exists():
            raise StableAPIError(detail="Email already exists.", code="email_taken", http_status=409)
        target.email = clean_email
    if is_active is not None:
        target.is_active = bool(is_active)
    if is_superuser is not None:
        target.is_superuser = bool(is_superuser)
    if password:
        from django.contrib.auth.password_validation import validate_password

        validate_password(password)
        target.set_password(password)
    target.save()
    return target


@transaction.atomic
def admin_delete_user(*, admin_user, target: User) -> None:
    """Delete a user and their memberships. Audit history is preserved
    (``AuditRun.created_by`` is SET_NULL). Guards: cannot delete self, cannot
    delete the last super admin."""
    _require_superuser(admin_user)
    if target.id == admin_user.id:
        raise StableAPIError(detail="You cannot delete your own account.", code="cannot_delete_self", http_status=409)
    if target.is_superuser and _superuser_count() <= 1:
        raise StableAPIError(detail="Cannot delete the last super admin.", code="last_superuser", http_status=409)
    target.delete()


# ─── Super-admin platform stats ──────────────────────────────────────────────


def admin_stats_payload() -> dict:
    """Aggregate platform + per-workspace counts. Counts only — no content."""
    from django.db.models import Count

    from audits.models import AuditRun
    from model_registry.models import ModelConnection
    from scenarios.models import Scenario, ScenarioSet

    projects = list(Project.objects.order_by("name"))
    project_ids = [p.id for p in projects]

    member_counts = dict(
        ProjectMembership.objects.filter(project__in=project_ids).values("project_id").annotate(n=Count("id")).values_list("project_id", "n")
    )
    run_counts = dict(
        AuditRun.objects.filter(project__in=project_ids).values("project_id").annotate(n=Count("id")).values_list("project_id", "n")
    )
    run_status_counts = {
        (project_id, status): n
        for project_id, status, n in AuditRun.objects.filter(project__in=project_ids).values("project_id", "status").annotate(n=Count("id")).values_list(
            "project_id", "status", "n"
        )
    }
    scenario_counts = dict(
        Scenario.objects.filter(project__in=project_ids).values("project_id").annotate(n=Count("id")).values_list("project_id", "n")
    )
    set_counts = dict(
        ScenarioSet.objects.filter(project__in=project_ids).values("project_id").annotate(n=Count("id")).values_list("project_id", "n")
    )
    connection_counts = dict(
        ModelConnection.objects.filter(project__in=project_ids).values("project_id").annotate(n=Count("id")).values_list("project_id", "n")
    )

    workspaces = []
    for project in projects:
        workspaces.append(
            {
                "id": project.id,
                "name": project.name,
                "slug": project.slug,
                "archived": project.archived,
                "member_count": member_counts.get(project.id, 0),
                "audit_run_count": run_counts.get(project.id, 0),
                "audit_completed": run_status_counts.get((project.id, "completed"), 0),
                "audit_failed": run_status_counts.get((project.id, "failed"), 0),
                "scenario_count": scenario_counts.get(project.id, 0),
                "scenario_set_count": set_counts.get(project.id, 0),
                "model_connection_count": connection_counts.get(project.id, 0),
                "created_at": project.created_at,
            }
        )

    run_status_totals = dict(AuditRun.objects.values("status").annotate(n=Count("id")).values_list("status", "n"))
    return {
        "platform": {
            "workspace_count": len(projects),
            "workspace_archived": sum(1 for p in projects if p.archived),
            "user_count": User.objects.count(),
            "user_active": User.objects.filter(is_active=True).count(),
            "audit_run_total": AuditRun.objects.count(),
            "audit_run_completed": run_status_totals.get("completed", 0),
            "audit_run_failed": run_status_totals.get("failed", 0),
            "audit_run_active": sum(
                n for status, n in run_status_totals.items() if status not in ("completed", "failed", "cancelled")
            ),
            "scenario_count": Scenario.objects.count(),
            "scenario_set_count": ScenarioSet.objects.count(),
            "model_connection_count": ModelConnection.objects.count(),
        },
        "workspaces": workspaces,
    }
