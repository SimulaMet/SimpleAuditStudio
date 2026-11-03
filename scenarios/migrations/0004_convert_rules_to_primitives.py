"""Convert stored agentic rules to custom checks (count, exists, none).

Self-contained on purpose: it must keep working after the rule engine changes.
Presets that can be expressed per unit become custom checks. Rules that need order or
grouping have no equivalent any more; they are kept as ``unsupported`` rules, which the
engine reports as INCONCLUSIVE, so they are never silently passed or dropped.
"""
from collections import Counter

from django.db import migrations

_CHECKS = {"count", "exists", "none", "all", "distinct"}


def _cond(field, op, value):
    return {"field": field, "op": op, "value": value}


def _tool_conditions(rule):
    conditions = [_cond("tool.name", "equals", rule["tool"])]
    return conditions + [_cond(f"tool.argument.{k}", "equals", v) for k, v in (rule.get("arguments") or {}).items()]


def _check(kind, where, check, expect=None):
    rule = {"type": "check", "select": {"kind": kind, "where": where}, "check": check}
    if expect is not None:
        rule["expect"] = expect
    return rule


def _compile(rule):
    """Checks equivalent to one preset, or None when it needs order or grouping."""
    kind = rule.get("type")
    if kind == "tool_called":
        low = rule.get("min", 0 if "max" in rule else 1)
        high = rule.get("max")
        if high is None:
            expect = {"op": "at_least", "value": low}
        elif low == 0:
            expect = {"op": "at_most", "value": high}
        else:
            expect = {"op": "between", "value": [low, high]}
        return [_check("tool", _tool_conditions(rule), "count", expect)]
    if kind == "tool_not_called":
        return [_check("tool", _tool_conditions(rule), "none")]
    if kind == "budget":
        where = [_cond("tool.name", "equals", rule["tool"])] if rule.get("tool") else []
        return [_check("tool", where, "count", {"op": "at_most", "value": rule["max"]})]
    if kind == "errors":
        return [_check("any", [_cond("span.error", "equals", True)], "count", {"op": "at_most", "value": rule["max"]})]
    if kind == "forbidden_values":
        scopes = rule.get("scope") or ["arguments", "results", "answer"]
        checks = []
        for value in rule["values"]:
            if "arguments" in scopes:
                checks.append(_check("tool", [_cond("tool.arguments", "contains", value)], "none"))
            if "results" in scopes:
                checks.append(_check("tool", [_cond("tool.result", "contains", value)], "none"))
            if "answer" in scopes:
                checks.append(_check("answer", [_cond("answer.text", "contains", value)], "none"))
        return checks
    if kind == "retrieval":
        checks = [_check("retrieval_document", [], "count", {"op": "at_least", "value": rule.get("min_documents", 1)})]
        for source in rule.get("sources", []):
            checks.append(_check("retrieval_document", [_cond("retrieval.source", "equals", source)], "exists"))
        return checks
    if kind == "sequence" and rule.get("mode") in ("any_order_others_allowed", "any_order_exact"):
        counts = Counter(rule.get("steps") or [])
        exact = rule["mode"] == "any_order_exact"
        checks = [_check("tool", [_cond("tool.name", "equals", name)], "count",
                         {"op": "equals" if exact else "at_least", "value": n}) for name, n in counts.items()]
        if exact:
            checks.append(_check("tool", [_cond("tool.name", "not_one_of", list(counts))], "none"))
        return checks
    return None


def _unsupported(rule):
    return {"type": "check", "check": "unsupported", "legacy": rule}


def convert_rules(rules):
    """A stored rule list with every rule as a custom check (or an unsupported marker)."""
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            out.append(rule)
        elif rule.get("type") == "check":
            if rule.get("check") == "all_of":
                out.extend(convert_rules(rule.get("parts") or []))
            elif rule.get("check") in _CHECKS:
                out.append(rule)
            else:
                out.append(_unsupported(rule))
        else:
            compiled = _compile(rule)
            out.extend(compiled if compiled is not None else [_unsupported(rule)])
    return out


def convert_metadata(metadata):
    agentic = (metadata or {}).get("agentic")
    if not agentic or not agentic.get("rules"):
        return metadata
    return {**metadata, "agentic": {**agentic, "rules": convert_rules(agentic["rules"])}}


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
        ("scenarios", "0003_scenariorevision_decision"),
    ]

    operations = [
        migrations.RunPython(convert_revisions, migrations.RunPython.noop),
    ]
