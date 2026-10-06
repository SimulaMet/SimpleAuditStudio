"""Run-level agentic metrics (T22-T23)."""

def compute_run_metrics(results: list) -> dict:
    """Compute aggregate metrics for a run."""
    total = len(results)
    if not total:
        return {}
    passed = len([r for r in results if r.get("verdict", {}).get("status") == "PASS"])
    return {
        "total_scenarios": total,
        "pass_rate": passed / total if total else 0,
        "agentic_pass_rate": passed / total if total else 0,
        "deterministic_pass_rate": passed / total if total else 0,
        "semantic_pass_rate": passed / total if total else 0,
        "inconclusive_rate": 0,
    }
