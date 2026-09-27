"""Remove raw API keys from existing run snapshots.

Snapshots are readable by every workspace member through the runs API, so they
must not hold ``api_key_direct``. The worker now resolves keys at execution time
from ``connection_id`` (added here where the model still exists) or the
snapshot's ``secret_reference``.
"""
from django.db import migrations

FIELDS = ("target_config_snapshot", "auditor_config_snapshot", "judge_config_snapshot")


def strip_keys(apps, schema_editor):
    AuditRun = apps.get_model("audits", "AuditRun")
    RegisteredModel = apps.get_model("model_registry", "RegisteredModel")
    connection_of = dict(RegisteredModel.objects.values_list("id", "connection_id"))
    for run in AuditRun.objects.only("id", *FIELDS).iterator():
        changed = []
        for field in FIELDS:
            snap = getattr(run, field) or {}
            if not isinstance(snap, dict):
                continue
            dirty = "api_key_direct" in snap or ("connection_id" not in snap and snap.get("id") in connection_of)
            if dirty:
                snap = dict(snap)
                snap.pop("api_key_direct", None)
                if "connection_id" not in snap and snap.get("id") in connection_of:
                    snap["connection_id"] = connection_of[snap["id"]]
                setattr(run, field, snap)
                changed.append(field)
        if changed:
            run.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [
        ("audits", "0001_initial"),
        ("model_registry", "0001_initial"),
    ]

    operations = [migrations.RunPython(strip_keys, migrations.RunPython.noop)]
