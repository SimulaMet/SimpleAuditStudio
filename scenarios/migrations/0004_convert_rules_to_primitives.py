"""Rules were converted to primitive checks here. The conversion code is gone, so this
step does nothing; it stays so the migration history of existing databases is intact."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("scenarios", "0003_scenariorevision_decision"),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
    ]
