from audits.agentic.metrics import compute_run_metrics, repetition_metrics
from audits.agentic.result_ui import format_agentic_result, group_checks_by_category


def test_repetition_metrics_uses_worst_severity_for_a_tie():
    result = {"reps": [{"severity": "pass"}, {"severity": "high"}]}

    assert repetition_metrics(result) == {
        "n_repetitions": 2, "aggregated_severity": "high", "agreement_rate": 0.5,
        "agreement_count": 1, "severity_distribution": {"high": 1, "pass": 1},
        "unstable": True,
    }


def test_compute_run_metrics_handles_wrapped_results_and_agentic_statuses():
    results = [
        {"result": {"severity": "pass", "agentic_evaluation": {"status": "PASS"}}},
        {"result": {"reps": [{"severity": "high"}, {"severity": "pass"}], "agentic_evaluation": {"status": "INCONCLUSIVE"}}},
    ]

    metrics = compute_run_metrics(results)

    assert metrics["total_scenarios"] == 2
    assert metrics["pass_rate"] == 0.5
    assert metrics["agentic_pass_rate"] == 0.5
    assert metrics["inconclusive_rate"] == 0.5
    assert metrics["severity_distribution"] == {"high": 1, "pass": 1}
    assert metrics["repeated_scenarios"] == 1
    assert metrics["unstable_scenarios"] == 1
    assert metrics["mean_agreement_rate"] == 0.5


def test_result_ui_groups_checks_and_normalizes_missing_verdict():
    result = format_agentic_result({"checks": [{"category": "tools", "id": "a"}, {"id": "b"}]})

    assert result["verdict"] == {"status": "INCONCLUSIVE", "summary": "No verdict explanation available."}
    assert list(result["checks"]) == ["tools", "other"]
    assert group_checks_by_category([None, {"category": "trace"}]) == {"trace": [{"category": "trace"}]}
