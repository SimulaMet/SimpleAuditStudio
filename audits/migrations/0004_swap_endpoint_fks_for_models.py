"""Swap AuditRun endpoint FKs (legacy ModelEndpoint) for RegisteredModel FKs.

Steps:
1. Add nullable target_model / auditor_model / judge_model FKs.
2. Backfill from the legacy endpoint FKs, matching on project + base_url + model_id.
3. Remove the legacy FK columns.
"""
import django.db.models.deletion
from django.db import migrations, models


def backfill_model_fks(apps, schema_editor):
    AuditRun = apps.get_model("audits", "AuditRun")
    ModelEndpoint = apps.get_model("model_registry", "ModelEndpoint")
    RegisteredModel = apps.get_model("model_registry", "RegisteredModel")

    for run in AuditRun.objects.all():
        for old_field, new_field in (
            ("target_endpoint_id", "target_model_id"),
            ("auditor_endpoint_id", "auditor_model_id"),
            ("judge_endpoint_id", "judge_model_id"),
        ):
            if getattr(run, new_field) is not None:
                continue
            ep_id = getattr(run, old_field)
            if not ep_id:
                continue
            ep = ModelEndpoint.objects.filter(pk=ep_id).first()
            if not ep:
                continue
            rm = RegisteredModel.objects.filter(
                project=ep.project,
                connection__base_url=ep.base_url,
                model_id=ep.model_id,
            ).first()
            if rm:
                setattr(run, new_field, rm.pk)
                run.save(update_fields=[new_field])


class Migration(migrations.Migration):

    dependencies = [
        ("audits", "0003_auditrun_updated_at"),
        ("model_registry", "0002_migrate_legacy_endpoints"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditrun",
            name="target_model",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="target_audit_runs",
                to="model_registry.registeredmodel",
            ),
        ),
        migrations.AddField(
            model_name="auditrun",
            name="auditor_model",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="auditor_audit_runs",
                to="model_registry.registeredmodel",
            ),
        ),
        migrations.AddField(
            model_name="auditrun",
            name="judge_model",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="judge_audit_runs",
                to="model_registry.registeredmodel",
            ),
        ),
        migrations.RunPython(backfill_model_fks, migrations.RunPython.noop),
        migrations.RemoveField(model_name="auditrun", name="target_endpoint"),
        migrations.RemoveField(model_name="auditrun", name="auditor_endpoint"),
        migrations.RemoveField(model_name="auditrun", name="judge_endpoint"),
        # Enforce NOT NULL now that every run has a backfilled model FK.
        migrations.AlterField(
            model_name="auditrun",
            name="target_model",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="target_audit_runs",
                to="model_registry.registeredmodel",
            ),
        ),
        migrations.AlterField(
            model_name="auditrun",
            name="auditor_model",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="auditor_audit_runs",
                to="model_registry.registeredmodel",
            ),
        ),
        migrations.AlterField(
            model_name="auditrun",
            name="judge_model",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="judge_audit_runs",
                to="model_registry.registeredmodel",
            ),
        ),
    ]
