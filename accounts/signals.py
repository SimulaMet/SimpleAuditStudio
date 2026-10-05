"""Keep Open WebUI workspace projections current after membership changes."""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from accounts.models import Project, ProjectMembership
from chat import sync


@receiver(post_save, sender=ProjectMembership, dispatch_uid="chat.project_membership_saved")
@receiver(post_delete, sender=ProjectMembership, dispatch_uid="chat.project_membership_deleted")
def membership_changed(sender, instance, **kwargs):
    transaction.on_commit(lambda: sync.schedule_push(f"membership {instance.project_id}"))


@receiver(post_save, sender=Project, dispatch_uid="chat.project_saved")
def project_changed(sender, instance, **kwargs):
    transaction.on_commit(lambda: sync.schedule_push(f"workspace {instance.pk}"))
