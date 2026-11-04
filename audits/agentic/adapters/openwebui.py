"""First-class Open WebUI support using the same GenAI trajectory normalizer.

Open WebUI emits standard gen_ai.* fields; its openwebui.* fields are metadata,
not an alternate semantic schema.
"""
from typing import Any

from ..schema import AgentTrajectory
from .generic import normalize as _normalize


def normalize(spans: list[dict[str, Any]]) -> AgentTrajectory:
    """Normalize Open WebUI's OTLP GenAI spans without attribute aliases."""
    return _normalize(spans)
