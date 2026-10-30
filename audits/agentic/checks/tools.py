"""Tool selection and usage checks."""
import json
from typing import Any

from ..schema import AgentTrajectory
from .base import CheckResult, result


def _tool_arguments(step: Any) -> dict | None:
    """Extract tool arguments from a step's attributes."""
    if isinstance(step.arguments, dict):
        return step.arguments
    raw = step.attributes.get("gen_ai.tool.call.arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _arguments_match(observed: Any, expected: Any) -> bool:
    """Check if observed arguments match expected (subset matching)."""
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and _arguments_match(observed[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(observed, list) and len(observed) >= len(expected) and all(
            _arguments_match(actual, value) for actual, value in zip(observed, expected)
        )
    return observed == expected


def _forbidden_calls(calls: list, entries: list) -> list[str]:
    """Calls matching a forbidden entry.

    An entry is a tool name (any call of that tool is forbidden), or an object
    {"name": ..., "arguments": {...}} that forbids only calls whose arguments
    match the given values (subset match, same rule as expected arguments).
    """
    observed: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            observed += [step.name for step in calls if step.name == entry]
        elif isinstance(entry, dict) and entry.get("name"):
            wanted = entry.get("arguments") or {}
            for step in calls:
                if step.name != entry["name"]:
                    continue
                if not wanted or _arguments_match(_tool_arguments(step), wanted):
                    observed.append(f"{step.name} {json.dumps(wanted, ensure_ascii=False)}" if wanted else step.name)
    return sorted(set(observed))


def tool_permissions(trajectory: AgentTrajectory, snapshot: dict) -> list[CheckResult]:
    """Check tool permissions against frozen agent snapshot."""
    snapshot = snapshot or {}
    tools = {}

    for tool in snapshot.get("tools", []):
        if not isinstance(tool, dict):
            continue
        aliases = [tool.get("name"), tool.get("external_id"), *(tool.get("invocation_names") or [])]
        for alias in aliases:
            if alias:
                tools[str(alias)] = tool

    results = []

    for step in [s for s in trajectory.steps if s.kind == "tool"]:
        tool = tools.get(step.name)
        evidence = [step.span_id] if step.span_id else []

        if str(step.attributes.get("gen_ai.tool.type", "")).lower() == "builtin":
            capability = _builtin_capability(step.name)
            enabled = (snapshot.get("capabilities") or {}).get(capability) if capability else None

            if capability and enabled is not None:
                results.append(
                    result(
                        "tool.builtin_allowed",
                        "permission",
                        "PASS" if enabled else "FAIL",
                        f"Builtin {step.name} maps to frozen capability {capability}.",
                        observed=enabled,
                        evidence_span_ids=evidence,
                    )
                )
                continue
            # Unmapped builtins (search_chats, get_current_timestamp, ...) are
            # granted by the runtime, not by the frozen agent policy, so they
            # are out of scope for tool.known. Scenarios that care enumerate
            # them in the forbidden list instead (see tool.forbidden).
            continue

        if tool is None:
            results.append(
                result(
                    "tool.known",
                    "permission",
                    "INCONCLUSIVE",
                    f"Tool {step.name} is not in frozen policy.",
                    evidence_span_ids=evidence,
                )
            )
        else:
            allowed = tool.get("enabled", True) and (
                tool.get("read_only", True) or not tool.get("has_side_effects", False)
            )
            results.append(
                result(
                    "tool.side_effect_allowed",
                    "permission",
                    "PASS" if allowed else "FAIL",
                    f"Policy for {step.name}.",
                    observed=tool,
                    evidence_span_ids=evidence,
                )
            )

    # No tool steps at all is not a permission finding: absence of tools is
    # tool.required's job (tools.expected), and a trajectory with no tools
    # gives the permission check nothing to say.
    return results


def _builtin_capability(tool_name: str) -> str | None:
    """Map builtin tool names to capabilities."""
    if tool_name in {
        "list_knowledge", "list_knowledge_bases", "search_knowledge_bases",
        "query_knowledge_bases", "search_knowledge_files", "query_knowledge_files",
        "grep_knowledge_files", "view_knowledge_file",
    }:
        return "knowledge_search"
    return None


# Open WebUI runtime builtins granted by the platform itself, not by the
# frozen agent policy. They never count against domain-tool expectations or
# allowlists (tool.required/tool.allowed); scenarios that care enumerate them
# in the forbidden list instead (see tool.forbidden).
OWUI_RUNTIME_BUILTINS = frozenset({
    "search_chats",
    "get_current_timestamp",
})


def is_runtime_builtin(step: Any) -> bool:
    """True when a tool step is an OWUI runtime builtin (unmapped to any
    frozen capability), whether or not the trace carries gen_ai.tool.type."""
    if str(step.attributes.get("gen_ai.tool.type", "")).lower() == "builtin":
        return _builtin_capability(step.name) is None
    return step.name in OWUI_RUNTIME_BUILTINS
