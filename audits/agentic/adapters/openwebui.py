"""OpenWebUI OTEL convention adapter."""

from typing import Any

from ..schema import AgentTrajectory
from .generic import normalize as _normalize


def _tool_arguments(attrs: dict[str, Any]) -> Any:
    raw = attrs.get("gen_ai.tool.call.arguments") or attrs.get("openwebui.tool.arguments")
    if isinstance(raw, str):
        import json

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return raw


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
        # OWUI has no native data-scope attribute; synthesize one from the
        # order identifier a tool call accessed, so policy.data_scope has
        # real evidence to check (order:ACME-1001 and friends).
        if attrs.get("gen_ai.tool.name") and not attrs.get("data_scope"):
            args = _tool_arguments(attrs)
            if isinstance(args, dict) and isinstance(args.get("order_id"), str):
                attrs["data_scope"] = [f"order:{args['order_id']}"]
        item["attributes"] = attrs
        mapped.append(item)
    return _normalize(mapped)
