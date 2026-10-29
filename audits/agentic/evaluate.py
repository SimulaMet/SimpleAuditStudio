"""Agentic evaluation orchestration (legacy—see orchestrator.py)."""
from dataclasses import asdict
from typing import Any

from .orchestrator import orchestrate_agentic_audit


def evaluate(
    trajectory: Any,
    expectations: Any = None,
    frozen_agent_snapshot: dict[str, Any] | None = None,
    *,
    llm=None,
    task: str = "",
    final_response: str = "",
) -> dict[str, Any]:
    """Legacy interface—delegates to orchestrator.

    Accepts AgenticExpectations (from validate_agentic_metadata) or dict.
    """
    try:
        # Convert AgenticExpectations to dict if needed
        if hasattr(expectations, "__dataclass_fields__"):
            scenario_metadata = {"agentic": asdict(expectations)}
        else:
            scenario_metadata = expectations or {}

        run_result = {}
        if final_response:
            run_result["judgment"] = {"final_response": final_response}
        return orchestrate_agentic_audit(
            run_result, trajectory, scenario_metadata, frozen_agent_snapshot,
            llm=llm, task=task,
        )
    except Exception as exc:  # noqa: BLE001 - evaluator must never break the base audit
        return {
            "status": "ERROR",
            "reason": str(exc),
            "checks": [{"id": "agentic.evaluate", "category": "system", "status": "ERROR", "summary": str(exc)}],
        }
