"""Stateful agent evaluation - verify side effects actually occurred."""
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class StateSnapshot:
    """State of a system at a point in time."""
    timestamp: float
    values: dict[str, Any]


class StateProbe(Protocol):
    """Protocol for evaluating agent side effects.

    Implementations capture state before/after execution and verify assertions.
    """

    def snapshot_before(self, context: dict[str, Any]) -> StateSnapshot:
        """Capture system state before agent execution."""
        ...

    def snapshot_after(self, context: dict[str, Any]) -> StateSnapshot:
        """Capture system state after agent execution."""
        ...

    def evaluate(
        self,
        assertions: list[dict[str, Any]],
        before: StateSnapshot,
        after: StateSnapshot,
    ) -> list[dict[str, Any]]:
        """Evaluate assertions against state snapshots.

        Returns list of check results.
        """
        ...


class InMemoryStateProbe:
    """In-memory tool state probe for testing."""

    def __init__(self):
        self.state = {}

    def snapshot_before(self, context: dict[str, Any]) -> StateSnapshot:
        import time
        return StateSnapshot(time.time(), dict(self.state))

    def snapshot_after(self, context: dict[str, Any]) -> StateSnapshot:
        import time
        return StateSnapshot(time.time(), dict(self.state))

    def set(self, key: str, value: Any) -> None:
        """Simulate tool setting a value."""
        self.state[key] = value

    def get(self, key: str) -> Any:
        """Simulate tool getting a value."""
        return self.state.get(key)

    def evaluate(
        self,
        assertions: list[dict[str, Any]],
        before: StateSnapshot,
        after: StateSnapshot,
    ) -> list[dict[str, Any]]:
        """Check state changes against assertions."""
        results = []

        for assertion in assertions:
            assertion_type = assertion.get("type")
            key = assertion.get("key")

            if assertion_type == "exists":
                exists_before = key in before.values
                exists_after = key in after.values
                status = "PASS" if exists_after else "FAIL"
                results.append({
                    "id": f"state.exists_{key}",
                    "status": status,
                    "summary": f"Key '{key}' exists: {exists_after}",
                })

            elif assertion_type == "changed":
                changed = before.values.get(key) != after.values.get(key)
                status = "PASS" if changed else "FAIL"
                results.append({
                    "id": f"state.changed_{key}",
                    "status": status,
                    "summary": f"Key '{key}' changed: {changed}",
                    "before": before.values.get(key),
                    "after": after.values.get(key),
                })

            elif assertion_type == "equals":
                expected = assertion.get("value")
                actual = after.values.get(key)
                status = "PASS" if actual == expected else "FAIL"
                results.append({
                    "id": f"state.equals_{key}",
                    "status": status,
                    "summary": f"Key '{key}' equals {expected}: {actual == expected}",
                    "expected": expected,
                    "observed": actual,
                })

            elif assertion_type == "no_mutation":
                mutated = before.values.get(key) != after.values.get(key)
                status = "PASS" if not mutated else "FAIL"
                results.append({
                    "id": f"state.no_mutation_{key}",
                    "status": status,
                    "summary": f"Key '{key}' unchanged: {not mutated}",
                })

        return results
