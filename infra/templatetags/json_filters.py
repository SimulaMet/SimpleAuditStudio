import json

from django import template

register = template.Library()


@register.filter
def jsonify(value):
    """Render a Python value as pretty-printed JSON for display in <pre> blocks."""
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2)


@register.filter
def jsonify_compact(value):
    """Render a Python value as compact single-line JSON (for input/textarea values)."""
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


@register.filter
def get_item(d, key):
    """Access a dict by key in a template: {{ my_dict|get_item:"key" }}."""
    if isinstance(d, dict):
        return d.get(key)
    return None


@register.filter
def editor_rules_json(agentic):
    """Rules for the rule editor, as JSON."""
    rules = (agentic or {}).get("rules") if isinstance(agentic, dict) else None
    rules = rules or []
    return json.dumps(rules, ensure_ascii=False) if rules else ""


@register.filter
def rule_sentence(rule):
    """A structured rule as one readable line."""
    from audits.agentic.rules import describe_rule

    return describe_rule(rule) if isinstance(rule, dict) else ""


@register.filter
def rule_code(rule):
    """A stored rule as read-only code, in its primitive (custom check) form."""
    from audits.agentic.rule_engine import check_code

    return check_code(rule) if isinstance(rule, dict) else ""


@register.filter
def rule_starters(_value=None):
    """Editor starter templates, as JSON: a label and the rules it adds."""
    from audits.agentic.rules import STARTERS

    return json.dumps(STARTERS, ensure_ascii=False).replace("<", "\\u003c")


@register.filter
def check_label(check_id):
    """A check id as a readable name ('Rule 1 · Tool must be called' for rule checks)."""
    from audits.agentic.rules import check_label as label

    return label(str(check_id))


@register.filter
def judge_dimensions(_value=None):
    """The semantic judge's canonical dimensions: name and description each."""
    from audits.agentic.semantic_judge import create_semantic_judge_dimensions

    return [{"name": d["name"], "description": d.get("description", "")} for d in create_semantic_judge_dimensions()]


@register.filter
def rule_field_catalog(_value=None):
    """The trace fields a custom check can read, for the rule editor's field picker."""
    from audits.agentic.rule_engine import field_catalog

    return json.dumps(field_catalog(), ensure_ascii=False)
