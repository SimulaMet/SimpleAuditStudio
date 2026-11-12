"""Make the seeded demo experiments public, once.

Only one demo experiment used to be public. The seed now makes them all
public (``infra/fixtures/demo_audit_results.json``), so a public deployment
shows every demo without sign-in; this brings workspaces seeded earlier in
line. It touches an experiment only when it has one of the seeded names and
every run in it is a seeded demo run, and, being a migration, it runs once:
an experiment someone makes private again afterwards stays private.
"""
from django.db import migrations

DEMO_EXPERIMENTS = (
    "Safety: does a hotter target change the verdicts?",
    "Health vs RAG scenarios",
    "Safety: graded by a decision model (Jev)",
)


def make_demo_experiments_public(apps, schema_editor):
    Experiment = apps.get_model("audits", "Experiment")
    for experiment in Experiment.objects.filter(name__in=DEMO_EXPERIMENTS, visibility="private"):
        runs = list(experiment.runs.all())
        if runs and all((run.runtime_metadata or {}).get("demo_seed") is True for run in runs):
            experiment.visibility = "public"
            experiment.save(update_fields=["visibility"])


class Migration(migrations.Migration):
    dependencies = [
        ("audits", "0012_perf_indexes"),
    ]

    operations = [
        migrations.RunPython(make_demo_experiments_public, migrations.RunPython.noop),
    ]
