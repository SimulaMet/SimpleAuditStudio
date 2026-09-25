"""Comparison engine: compare multiple audit runs with compatibility checks.

The spec requires:
- Identify potentially invalid comparisons (e.g., different scenario versions,
  different judges).
- Intersection-based comparison: only compare scenarios present in ALL runs.
- Do not silently compare incompatible experiments.
"""
from __future__ import annotations

from audits.events import ScenarioResult
from audits.models import AuditRun


class ComparisonIncompatible(Exception):
    """Raised when runs cannot be meaningfully compared."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def compare_runs(project, run_ids: list[int]) -> dict:
    """Compare multiple audit runs within a project.

    Returns a dict with:
      - warnings: list of human-readable compatibility warnings
      - compatible: bool (True if no hard incompatibilities)
      - runs: summary of each run
      - intersection: scenarios present in ALL runs (by scenario_key)
      - results: per-scenario comparison across runs
    """
    runs = list(AuditRun.objects.filter(id__in=run_ids, project=project).select_related(
        "scenario_set_version", "target_endpoint", "judge_endpoint", "auditor_endpoint"
    ))
    if len(runs) != len(run_ids):
        missing = set(run_ids) - {r.id for r in runs}
        raise ComparisonIncompatible([f"Run(s) {sorted(missing)} not found in this project."])

    warnings: list[str] = []

    # Check: same scenario set version?
    version_ids = {r.scenario_set_version_id for r in runs}
    if len(version_ids) > 1:
        vnums = sorted({r.scenario_set_version.version for r in runs})
        warnings.append(f"Scenario set versions differ: {', '.join('v'+str(v) for v in vnums)}. Results may not be directly comparable.")

    # Check: same judge?
    judge_ids = {r.judge_endpoint_id for r in runs}
    if len(judge_ids) > 1:
        judge_names = sorted({r.judge_endpoint.display_name for r in runs})
        warnings.append(f"Different judge models used: {', '.join(judge_names)}. Judge effects can dominate model effects — interpret with caution.")

    # Check: same auditor?
    auditor_ids = {r.auditor_endpoint_id for r in runs}
    if len(auditor_ids) > 1:
        auditor_names = sorted({r.auditor_endpoint.display_name for r in runs})
        warnings.append(f"Different auditor models used: {', '.join(auditor_names)}.")

    # Check: same SimpleAudit version?
    sa_versions = {r.simpleaudit_version for r in runs if r.simpleaudit_version}
    if len(sa_versions) > 1:
        warnings.append(f"Different SimpleAudit versions: {', '.join(sorted(sa_versions))}.")

    # Check: same n_repetitions?
    n_reps_set = {(r.generation_parameters_snapshot or {}).get("n_repetitions", 1) for r in runs}
    if len(n_reps_set) > 1:
        warnings.append(f"Different repetition counts: {', '.join(str(n)+'×' for n in sorted(n_reps_set))}. Stability metrics are not directly comparable.")

    # Build per-run result map: scenario_key -> result row.
    # version_item_id is a CharField (string of the PK), so we resolve keys via
    # the pinned ScenarioSetVersionItem rows.
    from scenarios.models import ScenarioSetVersionItem

    run_results: dict[int, dict[str, dict]] = {}
    for run in runs:
        # Map version_item_id (str) -> scenario key for this run's pinned version.
        item_map: dict[str, str] = {}
        if run.scenario_set_version_id:
            items = ScenarioSetVersionItem.objects.filter(
                version_id=run.scenario_set_version_id
            ).select_related("scenario")
            for item in items:
                item_map[str(item.id)] = item.scenario.key

        rows = ScenarioResult.objects.filter(run_id=str(run.id))
        m = {}
        for row in rows:
            key = item_map.get(row.version_item_id, f"item_{row.version_item_id}")
            r_data = row.result or {}
            # For repeated results, use aggregated severity
            if "reps" in r_data and isinstance(r_data.get("reps"), list):
                sev = r_data.get("aggregated_severity")
                agree = r_data.get("agreement_rate")
                summary = f"{sev} ({int(agree*100)}% agreement)" if agree is not None else sev
            else:
                sev = r_data.get("severity")
                summary = r_data.get("summary")
            m[key] = {
                "status": row.status,
                "attempts": row.attempts,
                "severity": sev,
                "summary": summary,
            }
        run_results[run.id] = m

    # Intersection: scenario keys present in ALL runs
    if run_results:
        key_sets = [set(m.keys()) for m in run_results.values()]
        common_keys = set.intersection(*key_sets) if key_sets else set()
    else:
        common_keys = set()

    # Per-scenario comparison
    results = []
    for key in sorted(common_keys):
        entry = {"scenario_key": key, "runs": {}}
        for run in runs:
            r = run_results.get(run.id, {}).get(key)
            entry["runs"][str(run.id)] = {
                "name": run.name,
                "target": run.target_endpoint.display_name if run.target_endpoint else None,
                **(r or {"status": "missing", "severity": None}),
            }
        results.append(entry)

    # Build inputs comparison: key parameters that differ between runs
    input_rows = []
    param_defs = [
        ("Target model", lambda r: r.target_endpoint.display_name if r.target_endpoint else "—"),
        ("Auditor model", lambda r: r.auditor_endpoint.display_name if r.auditor_endpoint else "—"),
        ("Judge model", lambda r: r.judge_endpoint.display_name if r.judge_endpoint else "—"),
        ("Scenario set version", lambda r: f"{r.scenario_set_version.scenario_set.name} (v{r.scenario_set_version.version})" if r.scenario_set_version else "—"),
        ("SimpleAudit version", lambda r: f"{r.simpleaudit_version} ({r.git_commit[:8]}…)" if r.simpleaudit_version and r.git_commit else (r.simpleaudit_version or "—")),
        ("Temperature (target)", lambda r: (r.generation_parameters_snapshot or {}).get("temperature_target", "—")),
        ("Temperature (auditor)", lambda r: (r.generation_parameters_snapshot or {}).get("temperature_auditor", "—")),
        ("Temperature (judge)", lambda r: (r.generation_parameters_snapshot or {}).get("temperature_judge", "—")),
        ("Max tokens", lambda r: (r.generation_parameters_snapshot or {}).get("max_tokens", "—")),
        ("Top-p", lambda r: (r.generation_parameters_snapshot or {}).get("top_p", "—")),
        ("Max turns", lambda r: (r.generation_parameters_snapshot or {}).get("max_turns", "—")),
        ("Repetitions", lambda r: str((r.generation_parameters_snapshot or {}).get("n_repetitions", 1)) + "×"),
        ("Concurrency", lambda r: (r.generation_parameters_snapshot or {}).get("concurrency", "—")),
        ("Language", lambda r: (r.generation_parameters_snapshot or {}).get("language", "—")),
    ]
    # Build cell data with optional URLs for linkable entities
    def _cell(text, url=None):
        return {"text": text or "—", "url": url}

    for label, getter in param_defs:
        cells = []
        for r in runs:
            text = getter(r)
            url = None
            if label == "Target model" and r.target_endpoint_id:
                url = f"/models/?highlight={r.target_endpoint_id}"
            elif label == "Auditor model" and r.auditor_endpoint_id:
                url = f"/models/?highlight={r.auditor_endpoint_id}"
            elif label == "Judge model" and r.judge_endpoint_id:
                url = f"/models/?highlight={r.judge_endpoint_id}"
            elif label == "Scenario set version" and r.scenario_set_version:
                url = f"/scenarios/?set={r.scenario_set_version.scenario_set_id}"
            cells.append(_cell(text, url))
        differs = len({c["text"] for c in cells}) > 1
        input_rows.append({"label": label, "cells": cells, "differs": differs})

    return {
        "compatible": len(warnings) == 0,
        "warnings": warnings,
        "runs": [
            {
                "id": r.id,
                "name": r.name,
                "status": r.status,
                "target": r.target_endpoint.display_name if r.target_endpoint else None,
                "judge": r.judge_endpoint.display_name if r.judge_endpoint else None,
                "scenario_set_version": f"v{r.scenario_set_version.version}" if r.scenario_set_version else None,
                "total_scenarios": r.total_scenarios,
                "successful_scenarios": r.successful_scenarios,
                "failed_scenarios": r.failed_scenarios,
            }
            for r in runs
        ],
        "inputs": input_rows,
        "intersection_count": len(common_keys),
        "results": results,
    }
