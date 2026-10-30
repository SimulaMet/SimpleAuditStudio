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
def tools_names(value):
    """Render a list of tool specs as comma-separated names.

    ``tools.expected`` items may be plain strings or rich dicts like
    ``{"name": ..., "min_calls": ..., "max_calls": ..., "arguments": ...}``.
    This extracts just the name so it can round-trip through a text input;
    per-tool call bounds and argument matching are authored via the read-only
    JSON preview (or import), not this field. Returns "" for empty/None.
    """
    if not value:
        return ""
    names = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name")
            names.append(str(name) if name else "")
        else:
            names.append(str(item))
    return ",".join(n for n in names if n)


@register.filter
def tool_entries_text(value):
    """Forbidden tool entries (names or name+arguments objects) as builder text."""
    from audits.agentic.tool_entries import format_tool_entries

    return format_tool_entries(value)


@register.filter
def editor_rules_json(agentic):
    """Rules for the rule editor: saved rules, then legacy settings converted to rules."""
    from audits.agentic.migration import editor_rules

    rules = editor_rules(agentic if isinstance(agentic, dict) else {})
    return json.dumps(rules, ensure_ascii=False) if rules else ""
