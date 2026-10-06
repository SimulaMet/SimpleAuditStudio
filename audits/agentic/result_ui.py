"""Small, presentation-ready transforms for agentic result UI."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def format_verdict_for_ui(verdict: dict[str, Any] | None) -> dict[str, str]:
    """Normalize verdict fields for a badge and accessible explanation."""
    verdict = verdict if isinstance(verdict, dict) else {}
    return {
        "status": str(verdict.get("status") or "INCONCLUSIVE").upper(),
        "summary": str(verdict.get("reason") or "No verdict explanation available."),
    }


def group_checks_by_category(checks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group checks while preserving input order and using a safe fallback."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for check in checks:
        if not isinstance(check, dict):
            continue
        grouped[str(check.get("category") or "other")].append(check)
    return dict(grouped)


def format_agentic_result(result: dict[str, Any] | None) -> dict[str, Any]:
    """Build the stable shape consumed by an agentic result panel."""
    result = result if isinstance(result, dict) else {}
    verdict = result.get("verdict")
    if not isinstance(verdict, dict):
        verdict = {"status": result.get("status"), "reason": result.get("reason")}
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    return {
        "verdict": format_verdict_for_ui(verdict),
        "checks": group_checks_by_category(checks),
        "trajectory_stats": result.get("trajectory_stats") or {},
        "judge_extension": result.get("judge_extension") or "",
    }
