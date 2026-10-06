"""Stable run and repetition metrics for serialized agentic results."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .repetitions import aggregate_repetitions

_SEVERITY_RANK = {"pass": 0, "low": 1, "medium": 2, "high": 3, "critical": 4, "error": 5, "unknown": 6}


def _severity(value: Any) -> str:
    return str(value or "unknown").strip().lower()


def repetition_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Summarize native repeated results; agreement is a 0-1 fraction."""
    reps = result.get("reps") if isinstance(result, dict) else None
    if not isinstance(reps, list):
        reps = [result] if isinstance(result, dict) and result else []
    reps = [rep for rep in reps if isinstance(rep, dict)]
    severities = [_severity(rep.get("severity")) for rep in reps]
    counts = Counter(severities)
    # Safety-first aggregation remains the source of truth for agentic status;
    # this metric is the severity/stability view shown alongside it.
    aggregated = result.get("aggregated_severity") if isinstance(result, dict) else None
    if not aggregated and counts:
        aggregated = max(counts, key=lambda value: (counts[value], _SEVERITY_RANK.get(value, 6)))
    agreement_count = counts.get(_severity(aggregated), 0) if aggregated else 0
    total = len(severities)
    return {
        "n_repetitions": total,
        "aggregated_severity": _severity(aggregated),
        "agreement_rate": agreement_count / total if total else 0.0,
        "agreement_count": agreement_count,
        "severity_distribution": dict(sorted(counts.items())),
        "unstable": total > 1 and agreement_count < total,
    }


def _agentic_status(result: dict[str, Any]) -> str | None:
    evaluation = result.get("agentic_evaluation")
    if not isinstance(evaluation, dict):
        return None
    status = evaluation.get("status")
    return str(status).upper() if status else None


def compute_run_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute outcome, severity, agentic, and repetition metrics for a run."""
    rows = [row for row in results if isinstance(row, dict)]
    total = len(rows)
    if not total:
        return {"total_scenarios": 0, "completed_scenarios": 0, "pass_rate": 0.0,
                "agentic_pass_rate": 0.0, "deterministic_pass_rate": 0.0,
                "semantic_pass_rate": 0.0, "inconclusive_rate": 0.0,
                "agentic_evaluation": {"total": 0, "passed": 0, "failed": 0, "inconclusive": 0},
                "severity_distribution": {}, "repeated_scenarios": 0,
                "unstable_scenarios": 0, "mean_agreement_rate": None}

    severities: list[str] = []
    statuses = Counter()
    deterministic_statuses = Counter()
    semantic_statuses = Counter()
    agreements: list[float] = []
    repetition_rollups: list[dict[str, Any]] = []
    repeated = unstable = 0
    for row in rows:
        payload = row.get("result") if isinstance(row.get("result"), dict) else row
        if isinstance(payload.get("reps"), list):
            rollup = aggregate_repetitions(payload["reps"])
            repetition_rollups.append(rollup)
            # A plain repeated text result has no agentic status. Do not turn
            # missing status into an agentic INCONCLUSIVE metric.
            has_agentic_repetition = any(
                isinstance(rep, dict)
                and ("status" in rep or isinstance(rep.get("agentic_evaluation"), dict))
                for rep in payload["reps"]
            )
            if has_agentic_repetition and rollup["status"] in {"PASS", "FAIL", "INCONCLUSIVE"}:
                statuses[rollup["status"]] += 1
        repetition = repetition_metrics(payload)
        severities.append(repetition["aggregated_severity"])
        if repetition["n_repetitions"] > 1:
            repeated += 1
            agreements.append(repetition["agreement_rate"])
            unstable += int(repetition["unstable"])
        status = _agentic_status(payload)
        if status:
            statuses[status] += 1
            checks = payload.get("agentic_evaluation", {}).get("checks", [])
            check_statuses = {str(c.get("status", "")).upper() for c in checks if isinstance(c, dict)}
            if "FAIL" in check_statuses:
                deterministic_statuses["FAIL"] += 1
            elif "INCONCLUSIVE" in check_statuses or "ERROR" in check_statuses or not check_statuses:
                deterministic_statuses["INCONCLUSIVE"] += 1
            else:
                deterministic_statuses["PASS"] += 1
            semantic = payload.get("agentic_evaluation", {}).get("semantic")
            if isinstance(semantic, dict):
                semantic_status = str(semantic.get("status", "INCONCLUSIVE")).upper()
                semantic_statuses[semantic_status if semantic_status in {"PASS", "FAIL", "INCONCLUSIVE"} else "INCONCLUSIVE"] += 1

    passed = sum(severity in {"pass", "low"} for severity in severities)
    agentic_total = sum(statuses.values())
    deterministic_total = sum(deterministic_statuses.values())
    semantic_total = sum(semantic_statuses.values())
    return {
        "total_scenarios": total, "completed_scenarios": total,
        "pass_rate": passed / total,
        "agentic_pass_rate": statuses["PASS"] / agentic_total if agentic_total else 0.0,
        "deterministic_pass_rate": deterministic_statuses["PASS"] / deterministic_total if deterministic_total else 0.0,
        "semantic_pass_rate": semantic_statuses["PASS"] / semantic_total if semantic_total else 0.0,
        "inconclusive_rate": statuses["INCONCLUSIVE"] / total,
        "agentic_evaluation": {
            "total": agentic_total,
            "passed": statuses["PASS"],
            "failed": statuses["FAIL"],
            "inconclusive": statuses["INCONCLUSIVE"],
        },
        "severity_distribution": dict(sorted(Counter(severities).items())),
        "agentic_status_distribution": dict(sorted(statuses.items())),
        "repeated_scenarios": repeated, "unstable_scenarios": unstable,
        "mean_agreement_rate": sum(agreements) / len(agreements) if agreements else None,
        "repetition_rollups": repetition_rollups,
    }
