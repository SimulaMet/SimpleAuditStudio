"""Provider-neutral trajectory data structures."""
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TrajectoryStep:
    """Normalized representation of one step in an agent execution trace."""
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    index: int = 0
    kind: Literal[
        "agent", "inference", "tool", "retrieval", "rerank",
        "guardrail", "approval", "handoff", "state", "workflow", "unknown"
    ] = "unknown"
    name: str = ""
    actor: str | None = None
    purpose: Literal["primary", "auxiliary"] = "primary"
    status: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    duration_ms: float | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: Any = None
    result: Any = None
    source_refs: list[str] = field(default_factory=list)
    handoff_to: str | None = None
    guardrail_decision: str | None = None
    approval_decision: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list = field(default_factory=list)

    def is_error(self) -> bool:
        """Check if this step represents an error."""
        status = (self.status or "").lower()
        return status in ("error", "failed", "exception")


@dataclass
class AgentTrajectory:
    """Complete normalized trajectory for one audit scenario."""
    steps: list[TrajectoryStep] = field(default_factory=list)
    normalization_version: str = "v1"

    @property
    def primary_steps(self) -> list[TrajectoryStep]:
        return [step for step in self.steps if step.purpose == "primary"]

    @property
    def auxiliary_steps(self) -> list[TrajectoryStep]:
        return [step for step in self.steps if step.purpose == "auxiliary"]

    def tools(self) -> list[TrajectoryStep]:
        """All tool call steps."""
        return [s for s in self.steps if s.kind == "tool"]

    def retrievals(self) -> list[TrajectoryStep]:
        """All retrieval steps."""
        return [s for s in self.steps if s.kind == "retrieval"]

    def guardrails(self) -> list[TrajectoryStep]:
        """All guardrail check steps."""
        return [s for s in self.steps if s.kind == "guardrail"]

    def approvals(self) -> list[TrajectoryStep]:
        """All approval/authorization steps."""
        return [s for s in self.steps if s.kind == "approval"]

    def handoffs(self) -> list[TrajectoryStep]:
        """All handoff/delegation steps."""
        return [s for s in self.steps if s.kind == "handoff"]

    def errors(self) -> list[TrajectoryStep]:
        """All error steps."""
        return [s for s in self.steps if s.is_error()]

    def sequence(self) -> list[str]:
        """Ordered list of step kinds for sequence matching."""
        return [s.kind for s in self.steps]

    def children(self, parent_span_id: str) -> list[TrajectoryStep]:
        """All direct children of a span."""
        return [s for s in self.steps if s.parent_span_id == parent_span_id]

    def ancestors(self, span_id: str) -> list[TrajectoryStep]:
        """All ancestors of a span (walking up parent chain)."""
        result = []
        current = None
        for s in self.steps:
            if s.span_id == span_id:
                current = s
                break
        while current and current.parent_span_id:
            for s in self.steps:
                if s.span_id == current.parent_span_id:
                    result.append(s)
                    current = s
                    break
            else:
                break
        return result

    def find(self, **filters) -> list[TrajectoryStep]:
        """Find steps matching the given criteria.

        Supported filters: kind, status, name, tool_name, etc.
        """
        result = []
        for step in self.steps:
            match = True
            for key, value in filters.items():
                if not hasattr(step, key):
                    match = False
                    break
                step_value = getattr(step, key)
                if isinstance(value, str):
                    if (step_value or "").lower() != value.lower():
                        match = False
                        break
                elif step_value != value:
                    match = False
                    break
            if match:
                result.append(step)
        return result
