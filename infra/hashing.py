"""Deterministic hashing helpers for immutable domain objects."""
import hashlib
import json


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def scenario_revision_hash(*, description: str, expected_behavior, test_prompt: str, metadata: dict,
                           severity_ceiling: str = "", documents=None, file_uri=None) -> str:
    payload = {
        "description": description or "",
        "expected_behavior": expected_behavior or [],
        "test_prompt": test_prompt or "",
        "severity_ceiling": severity_ceiling or "",
        "documents": documents or [],
        "file_uri": file_uri,
        # Metadata is part of the immutable execution contract. Keeping only
        # ``execution`` made agentic expectation edits invisible to hashes.
        "metadata": metadata or {},
    }
    return sha256_text(canonical_json(payload))


def scenario_set_version_hash(items: list[dict]) -> str:
    """Hash ordered set-version items.

    Each item should include at least:
    - position
    - scenario_id
    - scenario_key
    - revision
    - revision_content_hash
    """
    normalized = [
        {
            "position": item["position"],
            "scenario_id": item["scenario_id"],
            "scenario_key": item["scenario_key"],
            "revision": item["revision"],
            "revision_content_hash": item["revision_content_hash"],
        }
        for item in sorted(items, key=lambda entry: entry["position"])
    ]
    return sha256_text(canonical_json(normalized))
