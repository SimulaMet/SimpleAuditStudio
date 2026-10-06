"""Result UI rendering (T21)."""

def format_verdict_for_ui(verdict: dict) -> dict:
    """Format overall verdict for UI display."""
    return {"status": verdict.get("status"), "summary": verdict.get("reason")}

def group_checks_by_category(checks: list) -> dict:
    """Group checks for grouped display."""
    grouped = {}
    for check in checks:
        cat = check.get("category", "other")
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(check)
    return grouped
