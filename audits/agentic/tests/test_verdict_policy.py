"""Focused verdict-policy tests: semantic outcomes drive the composed verdict."""
from audits.agentic.verdict_policy import compute_overall_verdict


def _clean(**overrides):
    """A fully green evaluation (no deterministic or semantic signal)."""
    base = {
        "error": False,
        "missing_trace": False,
        "deterministic_fail": False,
        "semantic_fail": False,
        "response_fail": False,
        "has_inconclusive": False,
    }
    base.update(overrides)
    return base


def test_all_clean_is_pass():
    assert compute_overall_verdict(_clean()) == {"status": "PASS", "reason": "All checks passed"}


def test_semantic_fail_flips_composed_verdict_to_fail():
    """A semantic FAIL on an enabled criterion fails the run under gating."""
    result = compute_overall_verdict(_clean(semantic_fail=True))
    assert result["status"] == "FAIL"
    assert result["reason"] == "Semantic check failed"


def test_semantic_uncertain_on_enabled_criterion_is_inconclusive():
    """Insufficient observation (UNCERTAIN dimension) keeps the run INCONCLUSIVE,
    never PASS — the worker maps UNCERTAIN dimensions onto has_inconclusive."""
    result = compute_overall_verdict(_clean(has_inconclusive=True))
    assert result["status"] == "INCONCLUSIVE"


def test_deterministic_fail_beats_semantic_outcomes():
    """Deterministic FAILs keep precedence over semantic signals."""
    result = compute_overall_verdict(_clean(deterministic_fail=True, semantic_fail=True))
    assert result == {"status": "FAIL", "reason": "Deterministic check failed"}


def test_missing_trace_stays_inconclusive_even_with_semantic_fail():
    result = compute_overall_verdict(_clean(missing_trace=True, semantic_fail=True))
    assert result == {"status": "INCONCLUSIVE", "reason": "Required trace missing"}
