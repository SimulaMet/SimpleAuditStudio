"""Judge versions become criteria in an output format.

``rubric`` becomes ``base`` ("" = SimpleAudit's unnamed default, now
"default"), with the base's ``output``. A custom judge prompt becomes the
criteria: the base's format prompt is cut off its end when present. Run
snapshots get the new judge snapshot (spec plus resolved texts).
"""
from django.db import migrations, models


def _library():
    try:
        from simpleaudit.judges import JUDGE_CONFIGS
    except ImportError:
        return {}
    return JUDGE_CONFIGS


def _spec(rubric: str, probe_prompt: str, judge_prompt: str) -> dict:
    config = _library().get(rubric) or {}
    criteria = (judge_prompt or "").strip()
    fmt = (config.get("format_prompt") or "").strip()
    if criteria and fmt and criteria.endswith(fmt):
        criteria = criteria[: -len(fmt)].strip()
    if criteria == (config.get("criteria") or "").strip():
        criteria = ""
    return {
        "base": rubric or "default",
        "output": config.get("output") or "severity",
        "criteria": criteria,
        "probe_prompt": (probe_prompt or "").strip(),
        "options": {},
    }


def forwards(apps, schema_editor):
    JudgeVersion = apps.get_model("judges", "JudgeVersion")
    AuditRun = apps.get_model("audits", "AuditRun")
    for v in JudgeVersion.objects.all():
        spec = _spec(v.rubric, v.probe_prompt, v.judge_prompt)
        for key, value in spec.items():
            setattr(v, key, value)
        v.save(update_fields=list(spec))
    try:
        from judges.services import resolve
    except ImportError:
        resolve = None
    for run in AuditRun.objects.exclude(judge_config_snapshot={}).iterator():
        snap = dict(run.judge_config_snapshot or {})
        old = snap.get("judge") or {}
        if not old or "spec" in old:
            continue
        spec = _spec(
            old.get("rubric", ""),
            old.get("probe_prompt", "") if old.get("custom_probe_prompt") else "",
            old.get("judge_prompt", "") if old.get("custom_judge_prompt") else "",
        )
        new = {k: old[k] for k in ("judge_id", "version_id", "name", "version") if k in old}
        new.update(base=spec["base"], spec=spec)
        if resolve is not None:
            try:
                new.update(resolve(spec))
            except (ValueError, KeyError):   # a base SimpleAudit no longer has: spec only
                new.setdefault("output", spec["output"])
        snap["judge"] = new
        run.judge_config_snapshot = snap
        run.save(update_fields=["judge_config_snapshot"])


class Migration(migrations.Migration):
    dependencies = [
        ("judges", "0002_judge_model_per_run"),
        ("audits", "0004_judge_model_per_run"),
    ]

    operations = [
        migrations.AddField(
            model_name="judgeversion", name="base",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="judgeversion", name="output",
            field=models.CharField(default="severity", max_length=16),
        ),
        migrations.AddField(
            model_name="judgeversion", name="criteria",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="judgeversion", name="options",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name="judgeversion", name="rubric"),
        migrations.RemoveField(model_name="judgeversion", name="judge_prompt"),
    ]
