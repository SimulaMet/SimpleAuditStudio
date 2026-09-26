"""First-run seed logic — single source of truth.

Imports SimpleAudit scenario packs and creates default model connections.
Used by the ``seed_platform`` management command, which runs automatically on
web container boot (docker-compose) and in the HF Space ``start.sh``, and can
be run manually with ``--packs`` to import additional packs.

All functions are idempotent: existing data is skipped, never duplicated.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

DEFAULT_PACKS = ["safety", "rag", "health", "system_prompt"]

# Default model connections created on first run.
# (connection_name, provider, base_url, [(display_name, model_id), ...])
DEFAULT_MODELS = [
    (
        "OpenAI",
        "openai",
        "https://api.openai.com/v1",
        [
            ("GPT-4o", "gpt-4o"),
            ("GPT-4o Mini", "gpt-4o-mini"),
            ("GPT-4.1", "gpt-4.1"),
            ("GPT-4.1 Mini", "gpt-4.1-mini"),
            ("o3", "o3"),
            ("o3-mini", "o3-mini"),
        ],
    ),
]


def compute_content_hash(description: str, expected_behavior: list, test_prompt: str) -> str:
    payload = json.dumps(
        {
            "description": description,
            "expected_behavior": expected_behavior,
            "test_prompt": test_prompt,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def import_scenario_pack(project, user, pack_name: str, dry_run: bool = False):
    """Import one SimpleAudit built-in pack as a published ScenarioSet v1.

    Returns (scenario_set, version, status) where status is one of:
      - "imported": set + version created
      - "skipped":  set already exists with a published version
      - "empty":    pack has no scenarios
    ``version`` is None when status is not "imported".
    """
    from simpleaudit import get_scenarios

    from scenarios.models import (
        Scenario,
        ScenarioRevision,
        ScenarioSet,
    )
    from scenarios.services import publish_scenario_set_version

    set_name = f"SimpleAudit: {pack_name}"
    existing = ScenarioSet.objects.filter(project=project, name=set_name).first()
    if existing and existing.versions.exists():
        return existing, None, "skipped"

    scenarios_data = get_scenarios(pack_name)
    if not scenarios_data:
        return existing, None, "empty"

    if dry_run:
        return existing, None, "dry_run"

    # A set from an interrupted earlier run has no published version; drop it
    # and re-import (scenarios are reused by key, so nothing is duplicated).
    if existing:
        existing.delete()

    scenario_set = ScenarioSet.objects.create(
        project=project,
        name=set_name,
        description=(
            f"Imported from SimpleAudit built-in pack '{pack_name}'. "
            f"{len(scenarios_data)} scenarios."
        ),
        created_by=user,
    )

    scenario_ids = []
    for i, sc_data in enumerate(scenarios_data):
        name = sc_data.get("name", f"Scenario {i + 1}")
        description = sc_data.get("description", "")
        expected_behavior = sc_data.get("expected_behavior", [])
        test_prompt = sc_data.get("test_prompt", "")
        category = sc_data.get("category", pack_name)
        tags = sc_data.get("metadata", {}).get("tags", [pack_name])

        key = f"simpleaudit_{pack_name}_{i:03d}"

        scenario, _ = Scenario.objects.get_or_create(
            project=project,
            key=key,
            defaults={
                "title": name,
                "category": category,
                "tags": tags,
                "created_by": user,
            },
        )

        ScenarioRevision.objects.get_or_create(
            scenario=scenario,
            revision=1,
            defaults={
                "description": description,
                "expected_behavior": expected_behavior,
                "test_prompt": test_prompt,
                "content_hash": compute_content_hash(description, expected_behavior, test_prompt),
                "created_by": user,
            },
        )

        scenario_ids.append(scenario.id)

    version = publish_scenario_set_version(
        scenario_set=scenario_set,
        user=user,
        scenario_ids=scenario_ids,
    )
    return scenario_set, version, "imported"


def seed_default_model_connections(project, user) -> list[str]:
    """Create default model connections + models if missing.

    Returns a list of human-readable messages for logging.
    """
    from model_registry.models import ModelConnection, RegisteredModel

    messages = []
    for conn_name, provider, base_url, models in DEFAULT_MODELS:
        conn, created = ModelConnection.objects.get_or_create(
            project=project,
            name=conn_name,
            defaults={
                "provider": provider,
                "base_url": base_url,
                "enabled": True,
                "created_by": user,
            },
        )
        messages.append(
            f"Created model connection '{conn_name}'"
            if created
            else f"Model connection '{conn_name}' already exists"
        )

        for display_name, model_id in models:
            _, m_created = RegisteredModel.objects.get_or_create(
                connection=conn,
                project=project,
                model_id=model_id,
                defaults={
                    "display_name": display_name,
                    "enabled": True,
                    "default_parameters": {"temperature": 0.7, "max_tokens": 4096},
                },
            )
            if m_created:
                messages.append(f"  + {display_name} ({model_id})")
    return messages
