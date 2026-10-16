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


def _build_alias_map(snapshot: dict | None) -> dict[str, set[str]]:
    """Build a map from logical tool name → all known invocation aliases.

    Given the frozen agent config snapshot, every tool's name, external_id,
    and invocation_names are treated as interchangeable identifiers for the
    same underlying capability.  This lets scenario controls reference a
    logical alias (e.g. ``acme_lookup_order``) while the trace carries the
    real OpenWebUI function name (e.g. ``acme_get_order``).
    """
    alias_map: dict[str, set[str]] = {}
    if not snapshot:
        return alias_map
    for tool in snapshot.get("tools", []):
        if not isinstance(tool, dict):
            continue
        ids = {
            str(v)
            for v in [tool.get("name"), tool.get("external_id"), *(tool.get("invocation_names") or [])]
            if v
        }
        for ident in ids:
            alias_map.setdefault(ident, set()).update(ids)
    return alias_map


def tool_selection(trajectory: AgentTrajectory, expected: dict, snapshot: dict | None = None) -> list[CheckResult]:
    """Check tool calls against expected requirements."""
    calls = [s for s in trajectory.primary_steps if s.kind == "tool"]
    # Runtime builtins (search_chats, get_current_timestamp, ...) are granted
    # by the platform, not domain tools: they do not count against expected
    # or allowed lists. Explicit forbidden entries still catch them below.
    domain_calls = [s for s in calls if not is_runtime_builtin(s)]
    names = [s.name for s in domain_calls]
    required = expected.get("expected", [])
    out = []

    alias_map = _build_alias_map(snapshot)

    def _resolve(name: str) -> set[str]:
        """Return the set of span names that satisfy *name* (including itself)."""
        if name in alias_map:
            return alias_map[name]
        return {name}

    for item in required:
        name = item.get("name")
        acceptable_names = _resolve(name) if name else set()
        matching = [step for step in calls if step.name in acceptable_names]
        count = len(matching)
        # min_calls may be present as null — treat it as 1.
        lo, hi = item.get("min_calls"), item.get("max_calls")
        ok = count >= (1 if lo is None else lo) and (hi is None or count <= hi)

        out.append(
            result(
                "tool.required",
                "tool",
                "PASS" if ok else "FAIL",
                f"Required tool {name}: {count} call(s).",
                expected=item,
                observed=count,
                evidence_span_ids=[step.span_id for step in matching if step.span_id],
            )
        )

        expected_arguments = item.get("arguments")
        if expected_arguments is not None:
            # "all" (default): every call must carry the expected arguments.
            # "any": at least one call must — multi-turn audits legitimately
            # re-query with different user-supplied values across turns.
            arguments_mode = item.get("arguments_mode", "all")
            observed_arguments = [_tool_arguments(step) for step in matching]
            matches = [
                _arguments_match(value, expected_arguments)
                for value in observed_arguments
                if value is not None
            ]
            missing_capture = (
                not matching
                or (arguments_mode == "all" and len(matches) != len(matching))
                or (arguments_mode == "any" and not matches)
            )
            if arguments_mode == "any":
                argument_status = "PASS" if any(matches) else "FAIL"
            else:
                argument_status = "PASS" if matches and all(matches) else "FAIL"

            out.append(
                result(
                    "tool.arguments",
                    "tool",
                    "INCONCLUSIVE" if missing_capture else argument_status,
                    f"Arguments for {name} are {'unavailable' if missing_capture else 'checked'}.",
                    expected=expected_arguments,
                    observed=None if missing_capture else observed_arguments,
                    evidence_span_ids=[step.span_id for step in matching if step.span_id],
                )
            )

    forbidden = set(expected.get("forbidden", []))
    if forbidden:
        # Expand forbidden names through the alias map so that forbidding a
        # logical alias also catches all real invocation names.
        expanded_forbidden: set[str] = set()
        for fname in forbidden:
            expanded_forbidden.update(_resolve(fname))
        bad = expanded_forbidden.intersection({s.name for s in calls})
        out.append(
            result(
                "tool.forbidden",
                "tool",
                "FAIL" if bad else "PASS",
                "Forbidden tool called." if bad else "No forbidden tools called.",
                observed=sorted(bad),
            )
        )

    allowed = set(expected.get("allowed", []))
    if allowed:
        unknown = sorted(set(names) - allowed)
        out.append(result(
            "tool.allowed", "tool", "FAIL" if unknown else "PASS",
            "Only allowed tools were called." if not unknown else "A tool outside the allow-list was called.",
            expected=sorted(allowed), observed=unknown,
            evidence_span_ids=[step.span_id for step in calls if step.name in unknown and step.span_id],
        ))

    return out


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
