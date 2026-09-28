"""The judge model is picked per run / monitor; judges keep only rubric and prompts.

Monitors get their judge model from the judge version they used. The
"Default judge · <model>" judges made by 0003 (one per model) become one
"Safety Judge (SimpleAudit Default)" judge per workspace, since without the
model they are the same judge (its v1 keeps SimpleAudit's unnamed default).
"""
import django.db.models.deletion
from django.db import migrations, models


def forwards(apps, schema_editor):
    AuditRun = apps.get_model("audits", "AuditRun")
    Monitor = apps.get_model("audits", "Monitor")
    Judge = apps.get_model("judges", "Judge")
    JudgeVersion = apps.get_model("judges", "JudgeVersion")

    for monitor in Monitor.objects.all():
        version = monitor.judge_version or JudgeVersion.objects.filter(judge_id=monitor.judge_id).order_by("-version").first()
        Monitor.objects.filter(pk=monitor.pk).update(judge_model_id=version.model_id)

    backfilled = Judge.objects.filter(name__startswith="Default judge").order_by("project_id", "id")
    keep_by_project = {}
    for judge in backfilled:
        keep = keep_by_project.get(judge.project_id)
        if keep is None:
            name = "Safety Judge (SimpleAudit Default)"
            if Judge.objects.filter(project_id=judge.project_id, name=name).exclude(pk=judge.pk).exists():
                name = f"{name} ({judge.pk})"
            Judge.objects.filter(pk=judge.pk).update(name=name, description="SimpleAudit's default judge. v1: the unnamed default that graded earlier runs.")
            keep_by_project[judge.project_id] = (judge.pk, JudgeVersion.objects.get(judge_id=judge.pk, version=1).pk, name)
            continue
        keep_id, keep_version_id, _ = keep
        AuditRun.objects.filter(judge_version__judge_id=judge.pk).update(judge_version_id=keep_version_id)
        Monitor.objects.filter(judge_id=judge.pk).update(judge_id=keep_id, judge_version_id=keep_version_id)
        JudgeVersion.objects.filter(judge_id=judge.pk).delete()
        judge.delete()
    # Snapshots name the judge the run used.
    for keep_id, keep_version_id, name in keep_by_project.values():
        for run in AuditRun.objects.filter(judge_version_id=keep_version_id):
            snap = dict(run.judge_config_snapshot or {})
            snap["judge"] = {**(snap.get("judge") or {}), "judge_id": keep_id, "version_id": keep_version_id,
                             "name": name, "version": 1}
            AuditRun.objects.filter(pk=run.pk).update(judge_config_snapshot=snap)


class Migration(migrations.Migration):
    dependencies = [
        ("audits", "0003_judges"),
        ("judges", "0001_judges"),
        ("model_registry", "0002_descriptions"),
    ]

    operations = [
        migrations.AddField(
            model_name="monitor",
            name="judge_model",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.RESTRICT, related_name="judge_monitors",
                to="model_registry.registeredmodel",
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="monitor",
            name="judge_model",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="judge_monitors",
                to="model_registry.registeredmodel",
            ),
        ),
    ]
