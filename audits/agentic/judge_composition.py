"""Compose agentic judge profile by extending base judge."""
from typing import Any


def compose_agentic_judge(
    base_judge_spec: dict[str, Any],
    agentic_config: dict[str, Any],
) -> dict[str, Any]:
    """Compose base judge with agentic criteria extension.

    Base judge (safety/boundaries/etc) still runs for final response.
    Agentic extension evaluates process quality.
    """
    if not agentic_config.get("semantic_judge", {}).get("enabled"):
        return base_judge_spec

    composed = dict(base_judge_spec)
    criteria = list(base_judge_spec.get("criteria", []))

    # Add agentic criteria to existing ones
    agentic_criteria = agentic_config.get("semantic_judge", {}).get("criteria", [])
    criteria.extend(agentic_criteria)

    composed["criteria"] = criteria
    composed["_agentic_extension"] = True
    composed["_agentic_criteria"] = agentic_criteria

    return composed


def build_agentic_response_schema() -> dict[str, Any]:
    """Response schema for agentic judge output.

    Includes base severity plus agentic dimension scores.
    """
    return {
        "type": "object",
        "properties": {
            "severity": {
                "type": "string",
                "enum": ["critical", "high", "medium", "low", "pass"],
                "description": "Overall severity (base judge output)",
            },
            "summary": {"type": "string"},
            "agentic": {
                "type": "object",
                "properties": {
                    "task_completion": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "reason": {"type": "string"},
                            "evidence_span_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "tool_selection": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "reason": {"type": "string"},
                            "evidence_span_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "tool_result_handling": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "reason": {"type": "string"},
                            "evidence_span_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "grounding": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "reason": {"type": "string"},
                            "evidence_span_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "process_safety": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "reason": {"type": "string"},
                            "evidence_span_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "efficiency": {
                        "type": "object",
                        "properties": {
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "reason": {"type": "string"},
                            "evidence_span_ids": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "required": ["severity"],
    }


def extract_agentic_results(judge_output: dict[str, Any]) -> dict[str, Any]:
    """Extract agentic dimension results from judge output."""
    agentic = judge_output.get("agentic", {})
    if not agentic:
        return {}

    results = {}
    for dimension, data in agentic.items():
        if isinstance(data, dict):
            results[dimension] = {
                "score": data.get("score"),
                "reason": data.get("reason", ""),
                "evidence_span_ids": data.get("evidence_span_ids", []),
            }

    return results
