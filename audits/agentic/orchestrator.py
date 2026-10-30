"""Orchestrate complete agentic audit flow (integration of T01-T11)."""
from typing import Any

from .checks import apply_severity, rerank_checks, trace_integrity
from .checks.approvals import approval_checks
from .checks.budgets import budget_checks
from .checks.guardrails import guardrail_checks
from .checks.handoffs import handoff_checks
from .checks.policy import policy_checks
from .checks.retrieval import retrieval_requirements
from .checks.state import state_checks
from .checks.tools import tool_permissions, tool_selection
from .checks.trajectory import sequence_checks
from .judge_composition import compose_agentic_judge
from .schema import AgentTrajectory
from .schema_v2 import validate_agentic_metadata
from .semantic_judge import (
    PROMPT_VERSION,
    build_agentic_judge_prompt_extension,
    run_semantic_judge,
)
from .verdict_policy import compute_overall_verdict


def _final_response_from(run_result: dict[str, Any]) -> str:
    """Best-effort final response text from the engine result payload."""
    judgment = run_result.get("judgment") or {}
    for key in ("final_response", "response"):
        value = judgment.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def orchestrate_agentic_audit(
    run_result: dict[str, Any],
    trajectory: AgentTrajectory,
    scenario_metadata: dict[str, Any],
    agent_snapshot: dict[str, Any] | None = None,
    *,
    llm=None,
    task: str = "",
    expectations: list[str] | None = None,
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

    # Tool/permission policy is evaluated against the frozen agent snapshot
    # whenever one is available, independent of whether the scenario also
    # declares explicit tool-selection expectations.
    if agent_snapshot:
        perm_checks = tool_permissions(trajectory, agent_snapshot)
        all_checks.extend(perm_checks)

    if retrieval_config := agentic_config.get("retrieval"):
        retrieval_checks = retrieval_requirements(trajectory, retrieval_config, agent_snapshot)
        all_checks.extend(retrieval_checks)

    if traj_config := agentic_config.get("trajectory"):
        seq_checks = sequence_checks(trajectory, traj_config)
        all_checks.extend(seq_checks)

    for key, checker in (
        ("rerank", rerank_checks), ("budgets", budget_checks),
        ("guardrails", guardrail_checks), ("approvals", approval_checks),
        ("handoffs", handoff_checks), ("state", state_checks),
        ("policy", policy_checks),
    ):
        if config := agentic_config.get(key):
            all_checks.extend(checker(trajectory, config))

    if rules := agentic_config.get("rules"):
        from .checks.rules import rule_checks

        all_checks.extend(rule_checks(trajectory, rules))

    # Apply severity semantics (T05)
    severity_overrides = agentic_config.get("enforcement", {}).get("severity_overrides", {})
    for check in all_checks:
        apply_severity(check, severity_overrides, default_severity="medium")

    semantic_cfg = agentic_config.get("semantic_judge") or {}
    semantic_enabled = bool(semantic_cfg.get("enabled")) and bool(semantic_cfg.get("criteria"))
    enforcement_mode = (agentic_config.get("enforcement") or {}).get("mode", "gating")

    # Execute the semantic judge over the observed trajectory (T10). A
    # disabled judge leaves the ``semantic_judgement`` key absent so
    # consumers can tell "not configured" from an executed (possibly
    # UNCERTAIN) judgement; without an LLM endpoint the judgement degrades
    # to UNCERTAIN/llm_unavailable — it is never fabricated.
    semantic_judgement = None
    if semantic_enabled:
        semantic_judgement = run_semantic_judge(
            trajectory=trajectory,
            criteria=list(semantic_cfg["criteria"]),
            llm=llm,
            task=task,
            expectations=expectations,
            final_response=_final_response_from(run_result),
        )

    # Determine verdict (T15). Semantic FAIL fails the run only when the
    # enforcement mode is not advisory; insufficient observation (UNCERTAIN on
    # an enabled criterion) keeps the run INCONCLUSIVE. Deterministic FAIL
    # always stays FAIL regardless of semantic outcomes.
    semantic_dimensions = (semantic_judgement or {}).get("dimensions", {})
    evaluation = {
        "deterministic_checks": all_checks,
        "deterministic_fail": any(c.status == "FAIL" for c in all_checks),
        "has_inconclusive": any(c.status == "INCONCLUSIVE" for c in all_checks),
        "semantic_fail": (
            semantic_enabled
            and enforcement_mode != "advisory"
            and any(d.get("status") == "FAIL" for d in semantic_dimensions.values())
        ),
    }
    verdict = compute_overall_verdict(evaluation)
    if (
        verdict.get("status") == "PASS"
        and semantic_enabled
        and any(d.get("status") not in ("PASS", "FAIL") for d in semantic_dimensions.values())
    ):
        verdict = {"status": "INCONCLUSIVE", "reason": "Semantic observation insufficient"}

    # Build judge extension prompt (T10)
    judge_extension = ""
    if semantic_enabled:
        traj_summary = f"Trajectory: {len(trajectory.steps)} steps, {len(trajectory.errors())} errors"
        judge_extension = build_agentic_judge_prompt_extension(traj_summary)

    # Compose judge (T10)
    base_judge_spec = run_result.get("judgment", {}).get("judge_spec", {})
    composed_judge = compose_agentic_judge(base_judge_spec, agentic_config)

    # Return structured result. When the semantic judge is disabled (or no
    # criteria were configured) the ``semantic_judgement`` key is omitted so
    # consumers can distinguish "not configured" from an executed judgement
    # that came back UNCERTAIN.
    evaluation = {
        "status": verdict.get("status"),
        "reason": verdict.get("reason"),
        "checks": [
            {
                "id": c.id,
                "category": c.category,
                "status": c.status,
                "severity": c.severity,
                "summary": c.summary,
                "expected": c.expected,
                "observed": c.observed,
                "evidence_span_ids": c.evidence_span_ids,
                "details": c.details,
            }
            for c in all_checks
        ],
        "verdict": verdict,
        "judge_extension": judge_extension,
        "composed_judge": composed_judge,
        "prompt_version": PROMPT_VERSION if semantic_enabled else None,
        "trajectory_stats": {
            "total_steps": len(trajectory.steps),
            "tool_calls": len(trajectory.tools()),
            "errors": len(trajectory.errors()),
        },
    }
    if semantic_judgement is not None:
        evaluation["semantic_judgement"] = semantic_judgement
    return evaluation
