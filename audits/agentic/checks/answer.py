"""Helpers that read the agent's final answer and text values from the trace."""
from __future__ import annotations

import json

from ..schema import AgentTrajectory


def as_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def final_answer(trajectory: AgentTrajectory) -> str:
    """Output of the last primary inference step (the answer the user sees)."""
    answers = [step for step in trajectory.primary_steps if step.kind == "inference"]
    return as_text(answers[-1].attributes.get("gen_ai.output.messages")) if answers else ""
