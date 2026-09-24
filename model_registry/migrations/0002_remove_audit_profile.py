from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("model_registry", "0001_initial"),
        ("audits", "0002_remove_audit_profile"),
    ]

    operations = [
        migrations.DeleteModel(
            name="AuditProfile",
        ),
    ]
