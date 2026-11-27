"""Agentic verdict policy (gating-v1)."""


def compute_overall_verdict(evaluation_results: dict) -> dict:
    """Compute overall verdict per ordered precedence rules.

    Precedence (first match wins):
      1. error            -> ERROR
      2. missing_trace    -> INCONCLUSIVE
      3. deterministic_fail -> FAIL   (deterministic FAIL always stays FAIL)
      4. semantic_fail (enabled criterion, non-advisory enforcement) -> FAIL
      5. response_fail    -> FAIL
      6. has_inconclusive OR insufficient observation (semantic UNCERTAIN on an
         enabled criterion) -> INCONCLUSIVE
      7. otherwise        -> PASS

    Semantic outcomes only count when ``semantic_enabled`` is true; in
    advisory enforcement mode a semantic FAIL does not fail the run (it is
    surfaced in the judgement payload but maps to inconclusive at most).
    """
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


def before_semantic_judgement(evaluation: dict) -> dict:
    """A stored evaluation as it stood before a judge gave its semantic verdicts.

    The deterministic checks keep their results, the semantic judgement is
    deferred again, and the verdict is the one the checks alone give. While a
    semantic judgement is owed that is never PASS (``orchestrate_agentic_audit``).
    """
    checks = [c for c in evaluation.get("checks") or [] if isinstance(c, dict)]
    verdict = compute_overall_verdict({
        "error": evaluation.get("status") == "ERROR",
        "missing_trace": (evaluation.get("trace_completeness") or {}).get("complete") is False,
        "deterministic_fail": any(c.get("status") == "FAIL" for c in checks),
        "has_inconclusive": any(c.get("status") == "INCONCLUSIVE" for c in checks),
    })
    if verdict["status"] == "PASS":
        verdict = {"status": "INCONCLUSIVE", "reason": "Semantic observation insufficient"}
    return {
        **evaluation,
        "status": verdict["status"],
        "reason": verdict["reason"],
        "verdict": verdict,
        "semantic_judgement": {
            "status": "DEFERRED",
            "rationale": "Semantic dimensions evaluated post-hoc by worker.",
            "dimensions": {},
        },
    }
