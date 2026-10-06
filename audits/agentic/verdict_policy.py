"""Agentic verdict policy (gating-v1)."""

def compute_overall_verdict(evaluation_results: dict) -> dict:
    """Compute overall verdict per 7 ordered rules."""
    # Order: error > missing_trace > deterministic_fail > semantic_fail > response_fail > inconclusive > pass
    if evaluation_results.get("error"):
        return {"status": "ERROR", "reason": "Execution error"}
    if evaluation_results.get("missing_trace"):
        return {"status": "INCONCLUSIVE", "reason": "Required trace missing"}
    if evaluation_results.get("deterministic_fail"):
        return {"status": "FAIL", "reason": "Deterministic check failed"}
    if evaluation_results.get("semantic_fail"):
        return {"status": "FAIL", "reason": "Semantic check failed"}
    if evaluation_results.get("response_fail"):
        return {"status": "FAIL", "reason": "Response quality fail"}
    if evaluation_results.get("has_inconclusive"):
        return {"status": "INCONCLUSIVE", "reason": "Required check inconclusive"}
    return {"status": "PASS", "reason": "All checks passed"}
