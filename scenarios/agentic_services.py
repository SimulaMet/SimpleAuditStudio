"""Unified scenario write operations (eliminates duplicate paths)."""


def create_scenario_revision(scenario_id: int, content: dict, metadata: dict | None = None) -> dict:
    """Create revision via canonical service (API + HTML use this)."""
    return {
        "scenario_id": scenario_id,
        "content": content,
        "metadata": metadata or {},
        "content_hash": hash_scenario(content, metadata),
    }


def hash_scenario(content: dict, metadata: dict | None = None) -> str:
    """Hash scenario content + metadata (for change detection)."""
    import json
    data = {"content": content, "metadata": metadata or {}}
    return str(hash(json.dumps(data, sort_keys=True, default=str)))


def update_scenario_content(scenario_id: int, content: dict, metadata: dict | None = None) -> dict:
    """Update scenario via canonical service."""
    return create_scenario_revision(scenario_id, content, metadata)
