"""Adapter for already-normalized OTLP-like span dictionaries."""

from typing import Any

from ..schema import AgentTrajectory
from ..trajectory import normalize as normalize_spans


def normalize(spans: list[dict[str, Any]]) -> AgentTrajectory:
    """Normalize generic spans into :class:`AgentTrajectory`."""
    return normalize_spans(spans)
