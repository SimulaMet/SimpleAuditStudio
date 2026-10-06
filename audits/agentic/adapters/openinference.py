"""OpenInference span-kind adapter."""

from typing import Any

from ..schema import AgentTrajectory
from .generic import normalize as _normalize


def normalize(spans: list[dict[str, Any]]) -> AgentTrajectory:
    """Map OpenInference content fields, then normalize provider-neutrally."""
    mapped = []
    for span in spans:
        item = dict(span)
        attrs = dict(item.get("attributes") or {})
        aliases = {
            "tool.name": "gen_ai.tool.name",
            "tool.parameters": "gen_ai.tool.call.arguments",
            "tool.result": "gen_ai.tool.call.result",
            "input.value": "gen_ai.prompt",
            "output.value": "gen_ai.completion",
        }
        for source, target in aliases.items():
            if source in attrs and target not in attrs:
                attrs[target] = attrs[source]
        item["attributes"] = attrs
        mapped.append(item)
    return _normalize(mapped)
