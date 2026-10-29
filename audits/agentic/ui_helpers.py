"""UI helpers for agentic scenario creation and results display."""
from typing import Any


def render_scenario_form(scenario: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build form data for scenario creation/editing UI.

    Includes standard fields + agentic metadata sections.
    """
    if scenario is None:
        scenario = {}

    return {
        "standard": {
            "name": scenario.get("name", ""),
            "description": scenario.get("description", ""),
            "test_prompt": scenario.get("test_prompt", ""),
            "category": scenario.get("category", ""),
        },
        "agentic": {
            "enabled": bool(scenario.get("metadata", {}).get("agentic")),
            "trace": scenario.get("metadata", {}).get("agentic", {}).get("trace", {}),
            "tools": scenario.get("metadata", {}).get("agentic", {}).get("tools", {}),
            "trajectory": scenario.get("metadata", {}).get("agentic", {}).get("trajectory", {}),
            "enforcement": scenario.get("metadata", {}).get("agentic", {}).get("enforcement", {}),
        },
    }


def render_audit_result_summary(audit_result: dict[str, Any]) -> dict[str, Any]:
    """Format audit result for UI display (T21)."""
    checks = audit_result.get("checks", [])
    by_category = {}
    for check in checks:
        cat = check.get("category", "other")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(check)

    return {
        "overall_status": audit_result.get("status"),
        "overall_reason": audit_result.get("reason"),
        "checks_by_category": by_category,
        "stats": audit_result.get("trajectory_stats", {}),
        "verdict": audit_result.get("verdict", {}),
    }


def format_check_for_display(check: dict[str, Any]) -> dict[str, Any]:
    """Format single check for UI display."""
    severity_color = {
        "low": "green",
        "medium": "yellow",
        "high": "orange",
        "critical": "red",
    }.get(check.get("severity", "medium"), "gray")

    status_icon = {
        "PASS": "✓",
        "FAIL": "✗",
        "INCONCLUSIVE": "?",
        "ERROR": "⚠",
    }.get(check.get("status", "ERROR"), "?")

    return {
        "id": check.get("id"),
        "status": check.get("status"),
        "status_icon": status_icon,
        "severity": check.get("severity"),
        "severity_color": severity_color,
        "summary": check.get("summary"),
        "category": check.get("category"),
    }
