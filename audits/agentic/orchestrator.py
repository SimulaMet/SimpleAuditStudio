"""Orchestrate complete agentic audit flow (integration of T01-T11)."""
from typing import Any

from .schema import AgentTrajectory, TrajectoryStep
from .checks import trace_integrity, apply_severity
from .checks.tools import tool_selection, tool_permissions
from .checks.trajectory import sequence_checks
from .semantic_judge import build_agentic_judge_prompt_extension
from .judge_composition import compose_agentic_judge, extract_agentic_results
from .verdict_policy import compute_overall_verdict
from .schema_v2 import validate_agentic_metadata


def orchestrate_agentic_audit(
    run_result: dict[str, Any],
    trajectory: AgentTrajectory,
    scenario_metadata: dict[str, Any],
    agent_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute complete agentic audit flow.

    1. Validate scenario schema (T11)
    2. Extract trajectory (T04)
    3. Run deterministic checks (T05-T07)
    4. Compose semantic judge (T10)
    5. Compute verdict policy (T15)
    6. Return structured result

    Returns audit result with checks, judge extension, verdict.
    """
    agentic_config = scenario_metadata.get("agentic", {})

    # Validate schema (T11)
    if agentic_config:
        valid, errors = validate_agentic_metadata(agentic_config)
        if not valid:
            return {
                "status": "ERROR",
                "reason": f"Invalid agentic schema: {errors}",
                "checks": [],
            }

    # Run deterministic checks (T05-T07)
    all_checks = []

    trace_checks = trace_integrity(trajectory)
    all_checks.extend(trace_checks)

    if tools_config := agentic_config.get("tools"):
        tool_checks = tool_selection(trajectory, tools_config)
        all_checks.extend(tool_checks)

        if agent_snapshot:
            perm_checks = tool_permissions(trajectory, agent_snapshot)
            all_checks.extend(perm_checks)

    if traj_config := agentic_config.get("trajectory"):
        seq_checks = sequence_checks(trajectory, traj_config)
        all_checks.extend(seq_checks)

    # Apply severity semantics (T05)
    severity_overrides = agentic_config.get("enforcement", {}).get("severity_overrides", {})
    for check in all_checks:
        apply_severity(check, severity_overrides, default_severity="medium")

    # Determine verdict (T15)
    evaluation = {
        "deterministic_checks": all_checks,
        "deterministic_fail": any(c.status == "FAIL" for c in all_checks),
        "has_inconclusive": any(c.status == "INCONCLUSIVE" for c in all_checks),
    }

    verdict = compute_overall_verdict(evaluation)

    # Build judge extension prompt (T10)
    judge_extension = ""
    if agentic_config.get("semantic_judge", {}).get("enabled"):
        traj_summary = f"Trajectory: {len(trajectory.steps)} steps, {len(trajectory.errors())} errors"
        judge_extension = build_agentic_judge_prompt_extension(traj_summary)

    # Compose judge (T10)
    base_judge_spec = run_result.get("judgment", {}).get("judge_spec", {})
    composed_judge = compose_agentic_judge(base_judge_spec, agentic_config)

    # Return structured result
    return {
        "status": verdict.get("status"),
        "reason": verdict.get("reason"),
        "checks": [
            {
                "id": c.id,
                "category": c.category,
                "status": c.status,
                "severity": c.severity,
                "summary": c.summary,
            }
            for c in all_checks
        ],
        "verdict": verdict,
        "judge_extension": judge_extension,
        "composed_judge": composed_judge,
        "trajectory_stats": {
            "total_steps": len(trajectory.steps),
            "tool_calls": len(trajectory.tools()),
            "errors": len(trajectory.errors()),
        },
    }
