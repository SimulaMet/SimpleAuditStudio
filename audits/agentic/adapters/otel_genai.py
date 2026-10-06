"""OpenTelemetry GenAI semantic-convention adapter."""

from typing import Any

from ..schema import AgentTrajectory
from .generic import normalize as _normalize


def normalize(spans: list[dict[str, Any]]) -> AgentTrajectory:
    """Normalize OTEL GenAI spans into the common trajectory schema."""
    return _normalize(spans)
