from audits.agentic.repetitions import aggregate_repetitions


def _rep(status, severity=None):
    check = {"id": "scope", "status": "FAIL", "severity": severity} if severity else None
    return {"agentic_evaluation": {"status": status, "checks": [check] if check else []}}


def test_high_failure_in_one_repetition_cannot_be_hidden_by_passes():
    result = aggregate_repetitions([_rep("PASS"), _rep("FAIL", "high"), _rep("PASS")])
    assert result["status"] == "FAIL"
    assert result["worst_case_check_ids"] == ["scope"]


def test_repeated_failures_use_explicit_failure_rate_threshold():
    result = aggregate_repetitions([_rep("FAIL"), _rep("PASS"), _rep("PASS")])
    assert result["status"] == "PASS"
    assert result["failure_rate"] == 1 / 3


def test_low_evidence_is_inconclusive():
    result = aggregate_repetitions([_rep("INCONCLUSIVE"), _rep("ERROR")])
    assert result["status"] == "INCONCLUSIVE"
