"""Business logic for foundation operations.

Views should call these services rather than containing authorization or
bootstrap rules directly.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.text import slugify

from accounts.models import Project, ProjectMembership

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
