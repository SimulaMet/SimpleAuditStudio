"""Agentic evaluation orchestration."""
from .checks import evaluate_checks


def evaluate(trajectory, expectations, frozen_agent_snapshot):
    try:
        checks = evaluate_checks(trajectory, expectations, frozen_agent_snapshot)
        status = "FAIL" if any(c.status == "FAIL" for c in checks) else (
            "INCONCLUSIVE" if any(c.status == "INCONCLUSIVE" for c in checks) else "PASS")
        return {"status": status, "checks": [c.__dict__ for c in checks]}
    except Exception as exc:  # noqa: BLE001 - evaluator must never break the base audit
        return {"status": "ERROR", "checks": [{"id": "agentic.evaluate", "category": "system",
                "status": "ERROR", "summary": str(exc), "evidence_span_ids": [], "details": {}}]}
