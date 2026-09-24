"""Foundation models for users, projects, and project membership.

Phase 1 intentionally keeps the domain surface small. Scenario, model registry,
audit run, event, artifact, and comparison models are introduced in later phases
so migrations can be reviewed against the approved domain model.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models

# Django only auto-imports <app>/models.py. The domain models live in sibling
# modules, so they must be imported here to register with the app registry.
# Imported after the base classes below are defined to avoid circular imports.


class User(AbstractUser):
    """Studio user with stable identity for audit attribution.

    This is the AUTH_USER_MODEL. The inherited M2M fields are given explicit
    related_names so they do not clash with auth.User's reverse accessors.
    """

    # Stable WorkOS AuthKit user id; null for locally-created accounts.
    workos_user_id = models.CharField(max_length=64, unique=True, null=True, blank=True)

    groups = models.ManyToManyField(
        "auth.Group",
        blank=True,
        related_name="core_user_groups",
        verbose_name="groups",
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        blank=True,
        related_name="core_user_permissions",
        verbose_name="user permissions",
    )

    class Meta:
        db_table = "core_user"


class Project(models.Model):
    """Tenant-like scope for scenarios, models, audits, and comparisons."""

    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_project"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ProjectMembership(models.Model):
    """Role-based access to a project."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        AUDITOR = "auditor", "Auditor"
        VIEWER = "viewer", "Viewer"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "core_project_membership"
        constraints = [
            models.UniqueConstraint(fields=["project", "user"], name="unique_project_user"),
        ]
        ordering = ["project__name", "user__username"]

    def __str__(self) -> str:
        return f"{self.user.username} -> {self.project.slug}:{self.role}"



