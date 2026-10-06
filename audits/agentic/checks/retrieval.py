"""Retrieval and source checks."""
import json
import re
from typing import Any

from ..schema import AgentTrajectory
from .base import CheckResult, result

_KNOWLEDGE_RETRIEVAL_TOOLS = {
    "search_knowledge_files", "query_knowledge_files", "grep_knowledge_files",
}
_SOURCE_FIELDS = {
    "source", "source_name", "knowledge_base", "knowledge_base_id", "knowledge_id",
    "knowledge_ids", "collection", "collection_name", "file_id",
}


def _tool_arguments(step: Any) -> dict | None:
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
    raw = step.attributes.get("gen_ai.tool.call.result")
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def _captured_tool_sources(step) -> list[str]:
    captured = _captured_tool_result(step)
    if captured is not None:
        return _source_values(captured)

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
        captured = _captured_tool_result(step)
        if not isinstance(captured, dict):
            continue
        for knowledge in captured.get("knowledge_bases") or []:
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


def retrieval_requirements(
    trajectory: AgentTrajectory, expected: dict, snapshot: dict | None = None
) -> list[CheckResult]:
    """Check retrieval against expected requirements, resolving sources against
    a frozen agent/knowledge-base snapshot where available."""
    if not expected:
        return []

    steps = _retrieval_steps(trajectory)
    results = []

    if expected.get("required"):
        status = "INCONCLUSIVE" if not trajectory.steps else ("PASS" if steps else "FAIL")
        results.append(result(
            "retrieval.required", "retrieval", status,
            "Knowledge retrieval evidence is present." if steps else "Required retrieval evidence is unavailable or absent.",
            expected=True, observed=[step.name for step in steps],
            evidence_span_ids=[step.span_id for step in steps if step.span_id],
        ))

    expected_sources = expected.get("sources") or []
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

        expected_cf = {str(source).casefold() for source in expected_sources}
        for source in captured_sources:
            resolved = knowledge_names.get(source) or file_sources.get(source.casefold())
            if resolved is None and source.casefold() in expected_cf:
                resolved = source
            if resolved is None:
                resolved = default_source
            if resolved and resolved.casefold() not in {item.casefold() for item in canonical_sources}:
                canonical_sources.append(resolved)

        if not canonical_sources:
            source_status = "INCONCLUSIVE"
        else:
            source_status = "PASS" if any(
                str(source).casefold() in expected_cf for source in canonical_sources
            ) else "FAIL"

        results.append(result(
            "retrieval.source_allowed", "retrieval", source_status,
            "Expected source observed." if source_status == "PASS" else "Expected source was not captured or observed.",
            expected=expected_sources, observed=canonical_sources or None,
            evidence_span_ids=[step.span_id for step in steps if step.span_id],
        ))

    return results
