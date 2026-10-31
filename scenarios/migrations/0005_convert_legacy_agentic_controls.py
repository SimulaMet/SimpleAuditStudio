"""Keep agentic controls stored before the rule engine as unsupported rules.

Revisions saved before #65 keep their controls in the sections ``tools``,
``retrieval``, ``budgets``, ``trajectory``, ``policy``, ``rerank``, ``handoffs``,
``approvals``, ``guardrails`` and ``state``. The metadata validation now rejects
those sections, so such a revision could neither be evaluated nor saved. Each
section is moved, unchanged, into an ``unsupported`` rule of the shape migration
0004 writes, which the engine reports as INCONCLUSIVE, so no control is silently
passed or dropped. Nothing is converted to a custom check: the old checks and the
custom checks do not always agree on PASS and FAIL.
"""
from django.db import migrations

_RULE_KEYS = {"schema_version", "trace", "goal", "semantic_judge", "enforcement", "rules"}


def convert_metadata(metadata):
    agentic = (metadata or {}).get("agentic")
    if not isinstance(agentic, dict) or not set(agentic) - _RULE_KEYS:
        return metadata
    kept = {key: value for key, value in agentic.items() if key in _RULE_KEYS}
    legacy = [{"type": "check", "check": "unsupported", "legacy": {"type": key, key: agentic[key]}}
              for key in sorted(set(agentic) - _RULE_KEYS)]
    return {**metadata, "agentic": {**kept, "rules": list(kept.get("rules") or []) + legacy}}


def convert_revisions(apps, schema_editor):
    from infra.hashing import scenario_revision_hash

    ScenarioRevision = apps.get_model("scenarios", "ScenarioRevision")
    for revision in ScenarioRevision.objects.exclude(metadata={}).iterator():
        metadata = convert_metadata(revision.metadata)
        if metadata == revision.metadata:
            continue
        revision.metadata = metadata
        revision.content_hash = scenario_revision_hash(
            description=revision.description,
            expected_behavior=revision.expected_behavior,
            test_prompt=revision.test_prompt,
            severity_ceiling=revision.severity_ceiling,
            documents=revision.documents or [],
            file_uri=revision.file_uri,
            decision=revision.decision,
            metadata=metadata,
        )
        revision.save(update_fields=["metadata", "content_hash"])


class Migration(migrations.Migration):

    dependencies = [
        ("scenarios", "0004_convert_rules_to_primitives"),
    ]

    operations = [
        migrations.RunPython(convert_revisions, migrations.RunPython.noop),
    ]
