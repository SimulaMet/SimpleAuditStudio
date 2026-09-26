"""One-time migration: convert legacy ModelEndpoint rows into ModelConnection + RegisteredModel.

Idempotent: connections are matched on (project, base_url, provider) and models on
(connection, model_id), so re-running the migration is a no-op.
"""
from django.db import migrations


def migrate_legacy_endpoints(apps, schema_editor):
    ModelEndpoint = apps.get_model("model_registry", "ModelEndpoint")
    ModelConnection = apps.get_model("model_registry", "ModelConnection")
    RegisteredModel = apps.get_model("model_registry", "RegisteredModel")

    for ep in ModelEndpoint.objects.all():
        conn, _ = ModelConnection.objects.get_or_create(
            project=ep.project,
            base_url=ep.base_url,
            provider=ep.provider,
            defaults={
                "name": f"{ep.display_name} ({ep.provider})",
                "secret_reference": ep.secret_reference,
                "api_key_direct": ep.api_key_direct,
                "enabled": ep.enabled,
                "created_by": ep.created_by,
            },
        )
        RegisteredModel.objects.update_or_create(
            connection=conn,
            model_id=ep.model_id,
            defaults={
                "project": ep.project,
                "display_name": ep.display_name,
                "model_revision": ep.model_revision,
                "capabilities": ep.capabilities,
                "default_parameters": ep.default_parameters,
                "enabled": ep.enabled,
            },
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("model_registry", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(migrate_legacy_endpoints, noop),
    ]
