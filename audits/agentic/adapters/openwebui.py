"""OpenWebUI OTEL convention adapter."""

from typing import Any

from ..schema import AgentTrajectory
from .generic import normalize as _normalize


def normalize(spans: list[dict[str, Any]]) -> AgentTrajectory:
    """Map OpenWebUI trace payload to the provider-neutral trajectory."""
    mapped = []
    for span in spans:
        item = dict(span)
        attrs = dict(item.get("attributes") or {})
        aliases = {
            "openwebui.tool.name": "gen_ai.tool.name",
            "openwebui.tool.arguments": "gen_ai.tool.call.arguments",
            "openwebui.tool.result": "gen_ai.tool.call.result",
            "openwebui.agent.name": "gen_ai.agent.name",
            "openwebui.handoff.to": "gen_ai.handoff.to",
        }
        for source, target in aliases.items():
            if source in attrs and target not in attrs:
                attrs[target] = attrs[source]
        item["attributes"] = attrs
        mapped.append(item)
    return _normalize(mapped)
