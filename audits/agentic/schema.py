"""Provider-neutral trajectory data structures."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrajectoryStep:
    span_id: str | None
    kind: str
    name: str
    purpose: str = "primary"
    status: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTrajectory:
    steps: list[TrajectoryStep] = field(default_factory=list)

    @property
    def primary_steps(self) -> list[TrajectoryStep]:
        return [step for step in self.steps if step.purpose == "primary"]

    @property
    def auxiliary_steps(self) -> list[TrajectoryStep]:
        return [step for step in self.steps if step.purpose == "auxiliary"]
