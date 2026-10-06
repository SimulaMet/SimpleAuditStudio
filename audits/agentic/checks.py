"""Pure deterministic agentic checks."""
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .schema import AgentTrajectory


@dataclass
class CheckResult:
    id: str
    category: str
    status: str
    severity: str = ""
    summary: str = ""
    expected: Any = None
    observed: Any = None
    evidence_span_ids: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _result(check_id, category, status, summary, **kwargs):
    return CheckResult(check_id, category, status, summary=summary, **kwargs)


def _arguments_match(observed: Any, expected: Any) -> bool:
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


def _tool_arguments(step) -> dict | None:
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


def trace_integrity(trajectory: AgentTrajectory) -> list[CheckResult]:
    if not trajectory.steps:
        return [_result("trace.present", "trace", "INCONCLUSIVE", "No trace evidence captured.")]
    errors = [s for s in trajectory.steps if str(s.status).lower() in {"error", "failed"}]
    return [_result("trace.present", "trace", "PASS", "Trace evidence is present.",
                    observed=len(trajectory.steps)),
            _result("trace.no_error_spans", "trace", "FAIL" if errors else "PASS",
                    "Error spans detected." if errors else "No error spans detected.",
                    observed=len(errors), evidence_span_ids=[s.span_id for s in errors if s.span_id])]


def tool_selection(trajectory: AgentTrajectory, expected: dict) -> list[CheckResult]:
    calls = [s for s in trajectory.primary_steps if s.kind == "tool"]
    names = [s.name for s in calls]
    required = expected.get("expected", [])
    out = []
    for item in required:
        name = item.get("name")
        matching = [step for step in calls if step.name == name]
        count = len(matching)
        lo, hi = item.get("min_calls", 1), item.get("max_calls")
        ok = count >= lo and (hi is None or count <= hi)
        out.append(_result(
            "tool.required", "tool", "PASS" if ok else "FAIL",
            f"Required tool {name}: {count} call(s).", expected=item, observed=count,
            evidence_span_ids=[step.span_id for step in matching if step.span_id],
        ))
        expected_arguments = item.get("arguments")
        if expected_arguments is not None:
            observed_arguments = [_tool_arguments(step) for step in matching]
            missing_capture = not matching or any(value is None for value in observed_arguments)
            argument_status = (
                "INCONCLUSIVE" if missing_capture else
                "PASS" if all(_arguments_match(value, expected_arguments) for value in observed_arguments) else
                "FAIL"
            )
            out.append(_result(
                "tool.arguments", "tool", argument_status,
                f"Arguments for {name} are {'unavailable' if missing_capture else 'checked'}.",
                expected=expected_arguments,
                observed=None if missing_capture else observed_arguments,
                evidence_span_ids=[step.span_id for step in matching if step.span_id],
            ))
    forbidden = set(expected.get("forbidden", []))
    if forbidden:
        bad = forbidden.intersection(names)
        out.append(_result("tool.forbidden", "tool", "FAIL" if bad else "PASS",
                           "Forbidden tool called." if bad else "No forbidden tools called.", observed=sorted(bad)))
    return out


def _builtin_capability(tool_name: str) -> str | None:
    if tool_name in {
        "list_knowledge", "search_knowledge_files", "query_knowledge_files",
        "grep_knowledge_files",
    }:
        return "knowledge_search"
    return None


def tool_permissions(trajectory: AgentTrajectory, snapshot: dict) -> list[CheckResult]:
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
                results.append(_result(
                    "tool.builtin_allowed", "permission", "PASS" if enabled else "FAIL",
                    f"Builtin {step.name} maps to frozen capability {capability}.",
                    observed=enabled, evidence_span_ids=evidence,
                ))
                continue
        if tool is None:
            results.append(_result(
                "tool.known", "permission", "INCONCLUSIVE",
                f"Tool {step.name} is not in frozen policy.", evidence_span_ids=evidence,
            ))
        else:
            allowed = tool.get("enabled", True) and (tool.get("read_only", True) or not tool.get("has_side_effects", False))
            results.append(_result(
                "tool.side_effect_allowed", "permission", "PASS" if allowed else "FAIL",
                f"Policy for {step.name}.", observed=tool, evidence_span_ids=evidence,
            ))
    return results or [_result("tool.known", "permission", "INCONCLUSIVE", "No tool evidence captured.")]


_KNOWLEDGE_RETRIEVAL_TOOLS = {
    "search_knowledge_files", "query_knowledge_files", "grep_knowledge_files",
}
_SOURCE_FIELDS = {
    "source", "source_name", "knowledge_base", "knowledge_base_id", "knowledge_id",
    "knowledge_ids", "collection", "collection_name", "file_id",
}


def _source_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for part in value for item in _source_values(part)]
    if isinstance(value, dict):
        return [
            item
            for key, part in value.items()
            if key in _SOURCE_FIELDS
            for item in _source_values(part)
        ]
    return []


def _captured_tool_result(step) -> Any:
    result = step.attributes.get("gen_ai.tool.call.result")
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None
    return None


def _captured_tool_sources(step) -> list[str]:
    result = _captured_tool_result(step)
    if result is not None:
        return _source_values(result)

    raw = step.attributes.get("gen_ai.tool.call.result")
    if not isinstance(raw, str):
        return []

    sources = []
    pattern = r'"(?:source|file_id|filename)"\s*:\s*("(?:\\.|[^"\\])*")'
    for match in re.finditer(pattern, raw):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(value, str):
            sources.append(value)
    return sources


def _knowledge_file_sources(trajectory: AgentTrajectory) -> dict[str, str]:
    sources = {}
    for step in trajectory.steps:
        if step.kind != "tool" or step.name != "list_knowledge":
            continue
        result = _captured_tool_result(step)
        if not isinstance(result, dict):
            continue
        for knowledge in result.get("knowledge_bases") or []:
            if not isinstance(knowledge, dict) or not knowledge.get("name"):
                continue
            name = str(knowledge["name"])
            for file in knowledge.get("files") or []:
                if not isinstance(file, dict):
                    continue
                for value in (file.get("id"), file.get("filename"), file.get("name")):
                    if value:
                        sources[str(value).casefold()] = name
    return sources


def _retrieval_steps(trajectory: AgentTrajectory) -> list:
    return [
        step for step in trajectory.steps
        if step.kind == "retrieval"
        or (step.kind == "tool" and step.name in _KNOWLEDGE_RETRIEVAL_TOOLS)
    ]


def retrieval_checks(
    trajectory: AgentTrajectory, expectations: dict, snapshot: dict
) -> list[CheckResult]:
    if not expectations:
        return []
    steps = _retrieval_steps(trajectory)
    results = []
    if expectations.get("required"):
        status = "INCONCLUSIVE" if not trajectory.steps else ("PASS" if steps else "FAIL")
        results.append(_result(
            "retrieval.required", "retrieval", status,
            "Knowledge retrieval evidence is present." if steps else "Required retrieval evidence is unavailable or absent.",
            expected=True, observed=[step.name for step in steps],
            evidence_span_ids=[step.span_id for step in steps if step.span_id],
        ))

    expected_sources = expectations.get("sources") or []
    if expected_sources:
        direct_sources = []
        captured_sources = []
        for step in steps:
            attrs = step.attributes
            direct_sources.extend(_source_values(attrs.get("openwebui.retrieval.data_source")))
            args = _tool_arguments(step)
            if args:
                direct_sources.extend(_source_values(args))
            captured_sources.extend(_captured_tool_sources(step))
            documents = attrs.get("gen_ai.retrieval.documents") or []
            for document in documents if isinstance(documents, list) else [documents]:
                if isinstance(document, str):
                    try:
                        document = json.loads(document)
                    except json.JSONDecodeError:
                        continue
                captured_sources.extend(_source_values(document))
        knowledge_names = {
            str(kb.get("external_id")): str(kb.get("name"))
            for kb in (snapshot or {}).get("knowledge_bases", [])
            if isinstance(kb, dict) and kb.get("external_id") and kb.get("name")
        }
        file_sources = _knowledge_file_sources(trajectory)
        requested_names = {
            knowledge_names[source] for source in direct_sources if source in knowledge_names
        }
        if len(requested_names) == 1:
            default_source = next(iter(requested_names))
        elif not requested_names and len(knowledge_names) == 1:
            default_source = next(iter(knowledge_names.values()))
        else:
            default_source = None

        canonical_sources = []
        for source in direct_sources:
            resolved = knowledge_names.get(
                source, file_sources.get(source.casefold(), source)
            )
            if resolved.casefold() not in {item.casefold() for item in canonical_sources}:
                canonical_sources.append(resolved)
        expected = {str(source).casefold() for source in expected_sources}
        for source in captured_sources:
            resolved = knowledge_names.get(source) or file_sources.get(source.casefold())
            if resolved is None and source.casefold() in expected:
                resolved = source
            if resolved is None:
                resolved = default_source
            if resolved and resolved.casefold() not in {item.casefold() for item in canonical_sources}:
                canonical_sources.append(resolved)
        if not canonical_sources:
            source_status = "INCONCLUSIVE"
        else:
            expected = {str(source).casefold() for source in expected_sources}
            source_status = "PASS" if any(
                str(source).casefold() in expected for source in canonical_sources
            ) else "FAIL"
        results.append(_result(
            "retrieval.source_allowed", "retrieval", source_status,
            "Expected source observed." if source_status == "PASS" else "Expected source was not captured or observed.",
            expected=expected_sources, observed=canonical_sources or None,
            evidence_span_ids=[step.span_id for step in steps if step.span_id],
        ))
    return results


def evaluate_checks(trajectory, expectations, snapshot):
    results = trace_integrity(trajectory)
    tools = (expectations.tools if expectations else {})
    retrieval = (expectations.retrieval if expectations else {})
    results += tool_selection(trajectory, tools)
    results += tool_permissions(trajectory, snapshot)
    results += retrieval_checks(trajectory, retrieval, snapshot)
    return results
