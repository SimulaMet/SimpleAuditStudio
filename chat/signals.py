"""Model connections change in Studio, chat follows.

Connected from ``ChatConfig.ready()``, and only while the module is enabled.
The work itself is deferred and best-effort — see chat/sync.py.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from chat import sync


@receiver(post_save, sender="model_registry.ModelConnection", dispatch_uid="chat.connection_saved")
@receiver(post_delete, sender="model_registry.ModelConnection", dispatch_uid="chat.connection_deleted")
def connection_changed(sender, instance, **kwargs):
    _after_commit(f"connection {instance.pk}")


@receiver(post_save, sender="model_registry.RegisteredModel", dispatch_uid="chat.model_saved")
@receiver(post_delete, sender="model_registry.RegisteredModel", dispatch_uid="chat.model_deleted")
def registered_model_changed(sender, instance, **kwargs):
    # Which models a connection offers is part of what gets pushed, so a model
    # appearing or going away matters as much as the connection itself.
    _after_commit(f"model {instance.pk}")




def _after_commit(reason: str) -> None:
    """Push once the change is actually committed (and not at all if it isn't)."""
    transaction.on_commit(lambda: sync.schedule_push(reason))
