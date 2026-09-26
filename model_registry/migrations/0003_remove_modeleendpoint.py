"""Remove the deprecated legacy ModelEndpoint model.

All data was migrated to ModelConnection + RegisteredModel in 0002, and the
audit pipeline now references RegisteredModel directly (audits.0004).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("model_registry", "0002_migrate_legacy_endpoints"),
        ("audits", "0004_swap_endpoint_fks_for_models"),
    ]

    operations = [
        migrations.DeleteModel(name="ModelEndpoint"),
    ]
