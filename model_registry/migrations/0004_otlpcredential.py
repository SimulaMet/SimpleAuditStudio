import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("model_registry", "0003_modelconnection_shared_with_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OTLPCredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("auth_mode", models.CharField(choices=[("basic", "Basic Auth"), ("bearer", "Bearer Token")], default="basic", max_length=10)),
                ("username", models.CharField(blank=True, default="", max_length=250)),
                ("secret_hash", models.BinaryField()),
                ("salt", models.BinaryField()),
                ("target_id", models.CharField(max_length=250)),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("connection", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="otlp_credentials", to="model_registry.modelconnection")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="otlp_credentials", to="accounts.project")),
            ],
            options={
                "db_table": "core_otlp_credential",
                "ordering": ["project__name", "target_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="otlpcredential",
            constraint=models.UniqueConstraint(fields=["project", "target_id"], name="unique_target_id_per_project"),
        ),
    ]
