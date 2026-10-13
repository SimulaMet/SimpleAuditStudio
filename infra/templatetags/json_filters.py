import json

from django import template

register = template.Library()


@register.filter
def jsonify(value):
    """Render a Python value as a compact JSON string for use in input values."""
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
