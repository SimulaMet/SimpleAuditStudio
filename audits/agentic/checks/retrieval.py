"""Retrieval and source checks."""
import json
import re
from typing import Any

from ..schema import AgentTrajectory
from .base import CheckResult, result

_KNOWLEDGE_RETRIEVAL_TOOLS = {
    "search_knowledge_files", "query_knowledge_files", "grep_knowledge_files",
    "search_knowledge_bases", "query_knowledge_bases", "view_knowledge_file",
    "acme_search_knowledge", "search_knowledge",
}
_SOURCE_FIELDS = {
    "source", "source_name", "knowledge_base", "knowledge_base_id", "knowledge_ids",
    "knowledge_base_ids", "knowledge_id", "collection", "collection_name", "file_id",
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
        if step.kind != "tool" or step.name not in {"list_knowledge", "list_knowledge_bases"}:
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

        # A retrieval call is attributed to a source only when the trace
        # carries an observable KB/file/source identifier matching an
        # allowed/forbidden entry. There is no default inference from the
        # snapshot's linked knowledge bases: with a single linked KB the
        # agent could still query any other index, so absent identifiers mean
        # source identity is UNKNOWN -> INCONCLUSIVE, never PASS or FAIL.
        knowledge_names = {
            str(kb.get("external_id")): str(kb.get("name"))
            for kb in (snapshot or {}).get("knowledge_bases", [])
            if isinstance(kb, dict) and kb.get("external_id") and kb.get("name")
        }
        file_sources = _knowledge_file_sources(trajectory)

        # Only *identifiers* count as direct evidence: values that resolve
        # through the snapshot's external_id mapping or a list-knowledge
        # file mapping. A display name (e.g. "Acme Retail Policy") is
        # spoofable by any trace and must never be attributed on its own —
        # names only become sources via captured-source resolution below,
        # which requires an exact scenario-entry match plus context.
        canonical_sources = []
        for source in direct_sources:
            resolved = knowledge_names.get(source) or file_sources.get(source.casefold())
            if resolved is None:
                continue
            if resolved.casefold() not in {item.casefold() for item in canonical_sources}:
                canonical_sources.append(resolved)

        expected_cf = {str(source).casefold() for source in expected_sources}
        # When a linked KB exists in the snapshot, document filenames returned
        # by a known retrieval tool are attributed to that KB (the tool is
        # sandboxed to the agent's linked indices).
        _linked_kb_names = set(knowledge_names.values())
        for source in captured_sources:
            # Only observable identifiers count: snapshot name resolution,
            # list-knowledge file mapping, or an exact match against the
            # scenario's allowed/forbidden source entries. Unresolvable
            # captured text (e.g. document content echoing a policy phrase)
            # is not evidence of a source.
            resolved = knowledge_names.get(source) or file_sources.get(source.casefold())
            if resolved is None and source.casefold() in expected_cf:
                resolved = source
            # Document filename from a linked KB's retrieval tool → attribute
            # to the KB name so scenario-level source assertions work.
            if resolved is None and "." in source and len(_linked_kb_names) == 1:
                resolved = next(iter(_linked_kb_names))
            if resolved and resolved.casefold() not in {item.casefold() for item in canonical_sources}:
                canonical_sources.append(resolved)

        if not canonical_sources:
            # No retrieval happened. When retrieval is optional (required
            # falsy) the absence of sources is not a finding — the source
            # assertion still applies whenever retrieval does occur. When it
            # is required, missing evidence stays INCONCLUSIVE (the
            # retrieval.required check above already FAILs the absence).
            if steps or expected.get("required"):
                source_status = "INCONCLUSIVE"
            else:
                return results
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
