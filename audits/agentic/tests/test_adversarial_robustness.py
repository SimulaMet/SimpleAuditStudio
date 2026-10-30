"""Adversarial robustness cases for the agentic evaluation layer.

Synthetic trajectories and fakes only — no network, no LLM. Each case pins
either current-correct behavior or (where a real gap was found) the fixed
behavior:

1. Malformed LLM judge output -> UNCERTAIN, never PASS/FAIL, never raises.
2. Oversized evidence packet -> bounded size regardless of input volume.
3. Spoofed retrieval source name -> INCONCLUSIVE, not PASS.
4. Builtin-name collision (domain tool named like an OWUI builtin) ->
   pinned precedence in tool_selection/tool_permissions.
5. Repeated identical step names with equal timestamps -> deterministic
   ordering checks with stable first-occurrence tie-breaks.
"""
import json

from audits.agentic.checks.retrieval import retrieval_requirements
from audits.agentic.checks.tools import tool_permissions, tool_selection
from audits.agentic.checks.trajectory import sequence_checks
from audits.agentic.schema import AgentTrajectory, TrajectoryStep
from audits.agentic.semantic_judge import (
    build_evidence_packet,
    parse_semantic_judgement,
)


def _step(span, kind, name, **kwargs):
    return TrajectoryStep(span, kind, name, index=int(span[1:]) if span[1:].isdigit() else 0, **kwargs)


def _statuses(results):
    return {r.id: r.status for r in results}


# ---------------------------------------------------------------------------
# 1. Malformed LLM judge output
# ---------------------------------------------------------------------------

def test_malformed_judge_output_never_yields_pass_or_fail():
    criteria = ["task_completion", "grounding"]
    garbage_inputs = [
        "total garbage text with no structure",
        "",
        None,
        42,
        '{"dimensions": ',                          # truncated JSON
        'noise {"dimensions": {"task_completion": {"status": "PASS"',  # truncated mid-object
        '{"dimensions": {"task_completion": {"status": 5}}}',          # wrong type
        '{"dimensions": {"task_completion": {"status": ["FAIL"]}}}',   # status as list
        '{"dimensions": "not-a-dict"}',
        '{"no_dimensions_key": true}',
    ]
    for raw in garbage_inputs:
        dimensions = parse_semantic_judgement(raw, criteria)
        assert set(dimensions) == set(criteria)
        for name, entry in dimensions.items():
            assert entry["status"] == "UNCERTAIN", f"{raw!r} -> {name}: {entry['status']}"
            assert entry["rationale"], f"{raw!r} -> {name} has empty rationale"


def test_wellformed_lowercase_status_is_normalized_not_rejected():
    # Documents current behavior: statuses are case-insensitive.
    dimensions = parse_semantic_judgement(
        '{"dimensions": [{"name": "grounding", "status": "pass"}]}', ["grounding"]
    )
    assert dimensions["grounding"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# 2. Oversized evidence packet
# ---------------------------------------------------------------------------

def test_evidence_packet_is_bounded_with_large_tool_results_and_many_steps():
    big = "x" * 200_000
    steps = [
        _step(f"s{i}", "tool", f"t{i % 3}", arguments={"q": big}, result=big)
        for i in range(60)
    ]
    packet = build_evidence_packet(AgentTrajectory(steps), task="t" * 900)

    # Step count is capped; per-field excerpts are truncated.
    assert len(packet["tool_steps"]) <= 25
    assert packet["truncated"] is True
    for step_entry in packet["tool_steps"]:
        assert len(step_entry["arguments"]) < 1_000
        assert len(step_entry["result_excerpt"]) < 1_000
    assert len(packet["task"]) < 1_000

    # Total serialized packet stays bounded despite 12MB of raw input.
    assert len(json.dumps(packet)) < 200_000


# ---------------------------------------------------------------------------
# 3. Spoofed retrieval source
# ---------------------------------------------------------------------------

def _snapshot():
    return {"knowledge_bases": [
        {"id": 1, "name": "Acme Retail Policy", "external_id": "kb-acme"},
    ]}


def test_spoofed_data_source_name_without_identifier_is_inconclusive():
    # A document claims a source name that is neither a KB in the snapshot nor an
    # allowed scenario entry, and carries no observable identifier (snapshot
    # external_id, list-knowledge file mapping). Such a name is not evidence.
    trajectory = AgentTrajectory([
        _step("r1", "retrieval", "query", attributes={
            "gen_ai.retrieval.documents": [{"source": "Some Other KB"}],
        }),
    ])
    statuses = _statuses(retrieval_requirements(
        trajectory, {"required": True, "sources": ["Acme Retail Policy"]}, _snapshot()
    ))
    assert statuses["retrieval.required"] == "PASS"
    assert statuses["retrieval.source_allowed"] == "INCONCLUSIVE"


def test_spoofed_captured_source_name_is_trusted_only_as_exact_scenario_entry_match():
    # Pinned behavior: a captured source value that matches an expected
    # scenario entry exactly is accepted (documented exact-match channel).
    trajectory = AgentTrajectory([
        _step("r1", "tool", "query_knowledge_files", attributes={
            "gen_ai.tool.call.result": '{"source": "Acme Retail Policy", "content": "x"}',
        }),
    ])
    statuses = _statuses(retrieval_requirements(
        trajectory, {"required": True, "sources": ["Acme Retail Policy"]}, _snapshot()
    ))
    assert statuses["retrieval.source_allowed"] == "PASS"


def test_captured_source_name_not_matching_any_entry_is_inconclusive():
    # A captured source name matching no expected entry and not resolvable
    # through the snapshot/file mapping is unattributed -> INCONCLUSIVE.
    trajectory = AgentTrajectory([
        _step("r1", "tool", "query_knowledge_files", attributes={
            "gen_ai.tool.call.result": '{"source": "Unknown Vendor KB", "content": "x"}',
        }),
    ])
    statuses = _statuses(retrieval_requirements(
        trajectory, {"required": True, "sources": ["Acme Retail Policy"]}, _snapshot()
    ))
    assert statuses["retrieval.source_allowed"] == "INCONCLUSIVE"


def test_resolvable_external_id_still_passes_after_spoof_fix():
    # Regression guard: a genuine observable identifier keeps working.
    trajectory = AgentTrajectory([
        _step("r1", "retrieval", "query", attributes={
            "gen_ai.retrieval.documents": [{"source": "kb-acme"}],
        }),
    ])
    statuses = _statuses(retrieval_requirements(
        trajectory, {"required": True, "sources": ["Acme Retail Policy"]}, _snapshot()
    ))
    assert statuses["retrieval.source_allowed"] == "PASS"


# ---------------------------------------------------------------------------
# 4. Builtin name collision
# ---------------------------------------------------------------------------

def test_builtin_named_domain_tool_counts_against_required_but_not_allowlist():
    # Pinned precedence: tool.required matches by name across ALL calls
    # (including runtime builtins); the allowlist excludes runtime builtins
    # from domain_calls, so a builtin-named domain tool passes the allowlist
    # even when not listed there.
    trajectory = AgentTrajectory([
        _step("s1", "tool", "get_current_timestamp"),
        _step("s2", "tool", "lookup"),
    ])
    required = _statuses(tool_selection(
        trajectory, {"expected": [{"name": "get_current_timestamp"}]}
    ))
    assert required["tool.required"] == "PASS"

    allowed = _statuses(tool_selection(trajectory, {"allowed": ["lookup"]}))
    assert allowed["tool.allowed"] == "PASS"


def test_builtin_named_domain_tool_is_checked_by_frozen_policy_when_no_type_attr():
    # Pinned precedence: without gen_ai.tool.type=builtin, a step named like
    # an OWUI builtin is treated as a domain tool for permission purposes and
    # checked against the frozen policy entry of the same name.
    trajectory = AgentTrajectory([
        _step("s1", "tool", "get_current_timestamp"),
    ])
    snapshot = {"tools": [
        {"name": "get_current_timestamp", "enabled": True,
         "read_only": False, "has_side_effects": True},
    ], "capabilities": {}}
    statuses = _statuses(tool_permissions(trajectory, snapshot))
    assert statuses["tool.side_effect_allowed"] == "FAIL"


# ---------------------------------------------------------------------------
# 5. Trajectory ordering edge
# ---------------------------------------------------------------------------

def _identical_step(i):
    return TrajectoryStep(
        f"t{i}", "tool", "search", start_time=1000.0, end_time=1000.0, index=i
    )


def test_repeated_identical_steps_with_equal_timestamps_are_deterministic():
    # All steps share name AND timestamp; ordering derives solely from
    # recorded step order (first-occurrence tie-break), so results are stable.
    trajectory = AgentTrajectory([_identical_step(i) for i in range(4)])
    expected = {"required_sequence": [
        {"kind": "tool", "name": "search"}, {"kind": "tool", "name": "search"},
    ], "order_mode": "subsequence"}

    first = [(r.id, r.status) for r in sequence_checks(trajectory, expected)]
    second = [(r.id, r.status) for r in sequence_checks(trajectory, expected)]
    assert first == second == [("trajectory.subsequence", "PASS")]

    # Same multiset of steps in a different recorded order yields the same
    # verdict — the check depends only on relative order, not timestamps.
    reversed_trajectory = AgentTrajectory(list(reversed(trajectory.steps)))
    again = [(r.id, r.status) for r in sequence_checks(reversed_trajectory, expected)]
    assert again == [("trajectory.subsequence", "PASS")]

    # Partial-order self-constraint documents the first-occurrence
    # tie-break: before and after resolve to the same earliest step, so
    # before_idx < after_idx is false -> FAIL, deterministically.
    partial = _statuses(sequence_checks(trajectory, {
        "partial_order": [{"before": "tool", "after": "tool"}],
    }))
    assert partial["trajectory.order_tool_tool"] == "FAIL"
