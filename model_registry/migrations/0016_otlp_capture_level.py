from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("model_registry", "0015_knowledgereindex")]

    operations = [
        migrations.AddField(
            model_name="otlpcredential",
            name="capture_level",
            field=models.CharField(default="structural", max_length=20),
        ),
    ]
