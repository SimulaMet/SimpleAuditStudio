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
