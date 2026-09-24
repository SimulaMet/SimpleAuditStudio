from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("audits", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="auditrun",
            name="audit_profile",
        ),
    ]
