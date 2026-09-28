"""Runs and monitors are graded by a versioned Judge instead of a bare judge model.

Existing runs and monitors get a "Default judge" per judge model they used
(SimpleAudit's default rubric and prompts: exactly what graded them).
"""
import django.db.models.deletion
from django.db import migrations, models

DEFAULT_RUBRIC = {"rubric": "", "rubric_name": "SimpleAudit default", "output": "severity",
                  "probe_prompt": "", "judge_prompt": "", "custom_probe_prompt": False, "custom_judge_prompt": False}


def backfill(apps, schema_editor):
    AuditRun = apps.get_model("audits", "AuditRun")
    Monitor = apps.get_model("audits", "Monitor")
    RegisteredModel = apps.get_model("model_registry", "RegisteredModel")
    Judge = apps.get_model("judges", "Judge")
    JudgeVersion = apps.get_model("judges", "JudgeVersion")

    pairs = set(AuditRun.objects.values_list("project_id", "judge_model_id"))
    pairs |= set(Monitor.objects.values_list("project_id", "judge_model_id"))
    per_project = {}
    for project_id, model_id in pairs:
        per_project.setdefault(project_id, []).append(model_id)
    versions = {}
    for project_id, model_ids in per_project.items():
        for model_id in sorted(model_ids):
            model = RegisteredModel.objects.get(pk=model_id)
            name = "Default judge" if len(model_ids) == 1 else f"Default judge · {model.display_name or model.model_id}"
            judge = Judge.objects.create(
                project_id=project_id, name=name[:250],
                description="Created for runs graded before judges existed: SimpleAudit's default rubric.",
            )
            versions[(project_id, model_id)] = JudgeVersion.objects.create(
                judge=judge, version=1, model_id=model_id, note="Created from existing runs",
            )
    for run in AuditRun.objects.all().iterator():
        v = versions[(run.project_id, run.judge_model_id)]
        snap = dict(run.judge_config_snapshot or {})
        snap["judge"] = {"judge_id": v.judge_id, "version_id": v.pk, "name": v.judge.name, "version": 1, **DEFAULT_RUBRIC}
        AuditRun.objects.filter(pk=run.pk).update(judge_version=v, judge_config_snapshot=snap)
    for monitor in Monitor.objects.all():
        v = versions[(monitor.project_id, monitor.judge_model_id)]
        Monitor.objects.filter(pk=monitor.pk).update(judge_id=v.judge_id, judge_version=v)


class Migration(migrations.Migration):
    dependencies = [
        ("audits", "0002_strip_api_keys_from_snapshots"),
        ("judges", "0001_judges"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditrun",
            name="judge_version",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.RESTRICT, related_name="audit_runs", to="judges.judgeversion",
            ),
        ),
        migrations.AddField(
            model_name="monitor",
            name="judge",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.RESTRICT, related_name="monitors", to="judges.judge",
            ),
        ),
        migrations.AddField(
            model_name="monitor",
            name="judge_version",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name="monitors",
                to="judges.judgeversion",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="auditrun",
            name="judge_version",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="audit_runs", to="judges.judgeversion",
            ),
        ),
        migrations.AlterField(
            model_name="monitor",
            name="judge",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT, related_name="monitors", to="judges.judge",
            ),
        ),
        migrations.RemoveField(model_name="monitor", name="judge_model"),
    ]
