"""Retrieval and source checks."""
import json
import re
from typing import Any

from ..schema import AgentTrajectory

_KNOWLEDGE_RETRIEVAL_TOOLS = {
    "search_knowledge_files", "query_knowledge_files", "grep_knowledge_files",
    "search_knowledge_bases", "query_knowledge_bases", "view_knowledge_file",
    "search_knowledge",
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
        or (step.kind == "tool" and "gen_ai.retrieval.documents" in step.attributes)
        or (step.kind == "tool" and step.name in _KNOWLEDGE_RETRIEVAL_TOOLS)
    ]


