"""First-run seed logic — single source of truth.

Imports SimpleAudit scenario packs, creates default model connections and
starter judges.
Used by the ``seed_platform`` management command, which runs automatically on
web container boot (docker-compose) and in the HF Space ``start.sh``, and can
be run manually with ``--packs`` to import additional packs.

All functions are idempotent: existing data is skipped, never duplicated.
"""
from __future__ import annotations

import logging

from infra.hashing import scenario_revision_hash

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
                "content_hash": scenario_revision_hash(
                    description=description, expected_behavior=expected_behavior, test_prompt=test_prompt, metadata={}
                ),
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
                    "created_by": user,
                },
            )
            if m_created:
                messages.append(f"  + {display_name} ({model_id})")
    return messages


def seed_default_judges(project, user) -> list[str]:
    """Create SimpleAudit's judges in ``project`` if missing."""
    from judges.services import ensure_starter_judges

    made = ensure_starter_judges(project, user)
    return [f"Created judge '{name}'" for name in made] or ["Starter judges already exist"]


def seed_workspace(project, user) -> None:
    """What every new workspace starts with: SimpleAudit's standard scenario
    packs (DEFAULT_PACKS) and all its judges. Idempotent. ``user`` must be a
    member who may publish (the creator, an admin)."""
    for pack in DEFAULT_PACKS:
        import_scenario_pack(project, user, pack)
    seed_default_judges(project, user)


# ---------------------------------------------------------------------------
# Demo support agent — a self-contained RAG usecase (one agent + one
# knowledge base with two documents + one tool) so that /agents/,
# /agents/knowledge/ and /agents/tools/ are never all empty on a fresh
# workspace. Idempotent, and best-effort toward Open WebUI: if Open WebUI is
# not reachable, local reference rows are still created (with an empty
# external_id) so the agent page has content, and a later re-run will still
# complete the push.
# ---------------------------------------------------------------------------

DEMO_AGENT = {
    "name": "Support Refund Assistant",
    "description": (
        "Answers Acme Retail customers' refund, return and shipping questions "
        "from the policy knowledge base, and looks up order status with a tool."
    ),
    "system_prompt": (
        "You are the Acme Retail Support Refund Assistant. You help customers "
        "with refunds, returns, exchanges and shipping. Answer only from the "
        "Acme Retail policy knowledge base you can search; if the answer is not "
        "in the policy, say so rather than guessing. When a customer asks about "
        "a specific order, use the acme_lookup_order tool with the order id. "
        "Be concise, friendly and factual, and cite which policy section you "
        "are drawing from."
    ),
    "capabilities": {
        "knowledge_search": True,
        "file_read": True,
        "web_search": False,
        "url_fetch": False,
        "calculator": False,
        "code_execution": False,
        "memory": False,
        "subagents": False,
        "notifications": False,
    },
    "knowledge_base": {
        "name": "Acme Retail Policy",
        "description": (
            "Fictional Acme Retail policy corpus: refunds & returns and "
            "shipping & delivery."
        ),
    },
    # (fixture filename under infra/fixtures/sample_docs/, display title)
    "docs": [
        ("refunds_and_returns_policy.md", "Acme Retail Refunds & Returns Policy"),
        ("shipping_and_delivery_guide.md", "Acme Retail Shipping & Delivery Guide"),
    ],
    "tool": {
        "name": "Acme Order Lookup",
        # "custom" = a custom OpenWebUI function (see Tool.ToolType)
        "type": "custom",
        "description": (
            "Looks up a fictional Acme Retail order by order id (status, "
            "items, shipping speed, refund window)."
        ),
        # Stable id for the Open WebUI function; deterministic so re-runs map
        # to the same function instead of creating a new one each time.
        "owui_tool_id": "acme_order_lookup",
        "content_file": "order_lookup.py",
    },
}


def _fixture_dir(subdir: str) -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent / "fixtures" / subdir)


def _demo_base_model(project):
    """The registered model the demo agent answers with; prefer GPT-4o."""
    from model_registry.models import RegisteredModel

    model = (
        RegisteredModel.objects.filter(
            project=project, model_id__in=["gpt-4o", "gpt-4o-mini"]
        )
        .order_by("id")
        .first()
    )
    if model is None:
        model = RegisteredModel.objects.filter(project=project).order_by("id").first()
    return model


def _push_to_openwebui(api, log) -> tuple[str, str]:
    """Create the demo KB + documents + tool in Open WebUI.

    Returns (kb_external_id, tool_external_id); an empty string / None when
    that part of the push did not land.
    """
    from pathlib import Path

    log(f"Pushing demo knowledge base to Open WebUI ({api.base_url}).")

    # Knowledge base: reuse by name if a previous run created it.
    kb_name = DEMO_AGENT["knowledge_base"]["name"]
    existing_kb = next(
        (kb for kb in api.knowledge_bases() if kb.get("name") == kb_name), None
    )
    if existing_kb:
        kb_id = existing_kb.get("id")
        log(f"  • KB '{kb_name}' already in Open WebUI; reusing {kb_id}")
    else:
        created = api.create_knowledge_base(
            kb_name, DEMO_AGENT["knowledge_base"]["description"]
        )
        kb_id = created.get("id") if isinstance(created, dict) else None
        if not kb_id:
            log("  ! Open WebUI did not return a KB id; docs will not be linked.")
    kb_external_id = kb_id or ""

    # Documents: upload raw, then link into the KB.
    docs_dir = _fixture_dir("sample_docs")
    for filename, _title in DEMO_AGENT["docs"]:
        path = Path(docs_dir) / filename
        if not path.exists():
            log(f"  ! demo doc missing, skipped: {filename}")
            continue
        try:
            file_row = api.upload_file(filename, path.read_bytes(), "text/markdown")
            file_id = file_row.get("id") if isinstance(file_row, dict) else None
            if file_id and kb_id:
                api.add_file_to_knowledge_base(kb_id, file_id)
                log(f"  • uploaded + linked {filename}")
            else:
                log(f"  ! could not link {filename} (no file id or no KB id)")
        except Exception as exc:  # noqa: BLE001 - best-effort seed
            log(f"  ! upload failed for {filename}: {exc}")

    # Tool: register the toolkit in Open WebUI's Tools workspace.
    tool = DEMO_AGENT["tool"]
    tool_content = (Path(_fixture_dir("sample_tools")) / tool["content_file"]).read_text()
    tool_external_id: str | None = None
    try:
        existing_tool = next(
            (t for t in api.tools() if t.get("id") == tool["owui_tool_id"]),
            None,
        )
        if existing_tool:
            log("  • tool already in Open WebUI; skipping create")
        else:
            api.create_tool(
                tool["owui_tool_id"],
                tool["name"],
                tool_content,
                tool["description"],
            )
            log(f"  • created tool {tool['owui_tool_id']}")
        tool_external_id = tool["owui_tool_id"]
    except Exception as exc:  # noqa: BLE001 - best-effort seed
        log(f"  ! tool push failed: {exc}")

    return kb_external_id, tool_external_id


def seed_demo_agent(project, user, log=logger.info) -> dict[str, str]:
    """Create the demo Support Refund Assistant for a project.

    One agent wired to one knowledge base (two policy documents) and one
    order-lookup tool, and pushed to Open WebUI so the knowledge/tools pages
    have real content. Local reference rows are always created; their
    ``external_id`` is set only when the Open WebUI push succeeded, so a run
    without Open WebUI still leaves a coherent (locally-visible) agent, and a
    later re-run fills in the external id. Idempotent: re-running reuses the
    existing agent/KB/tool and never duplicates them.

    Returns a status dict, e.g. ``{"agent": "created", "openwebui": "pushed"}``.
    """
    from model_registry.models import (
        Agent,
        KnowledgeBase,
        Tool,
    )

    status: dict[str, str] = {"agent": "skipped", "openwebui": "skipped"}
    base_model = _demo_base_model(project)
    if base_model is None:
        log("Demo agent: no registered model for the project yet; skipping.")
        status["agent"] = "no-model"
        return status

    # --- best-effort push to Open WebUI -------------------------------------
    kb_external_id: str = ""
    tool_external_id: str = ""
    try:
        from chat import config as chat_config
        from chat.api import ChatAPI, ChatAPIError

        if getattr(chat_config, "ENABLED", False):
            api = ChatAPI.as_user(user)
            kb_external_id, tool_external_id = _push_to_openwebui(api, log)
            status["openwebui"] = "pushed"
        else:
            log("Demo agent: chat disabled; creating local reference rows only.")
            status["openwebui"] = "disabled"
    except ChatAPIError:
        log("Open WebUI push skipped (could not reach Open WebUI).")
        status["openwebui"] = "unavailable"
    except Exception as exc:  # noqa: BLE001 - seed must not fail on chat
        log(f"Open WebUI push skipped: {exc}")
        status["openwebui"] = "unavailable"

    # --- local reference rows (always created; idempotent) ------------------
    kb_meta = DEMO_AGENT["knowledge_base"]
    knowledge_base, created = KnowledgeBase.objects.get_or_create(
        project=project,
        name=kb_meta["name"],
        defaults={
            "description": kb_meta["description"],
            "trust_level": KnowledgeBase.TrustLevel.HIGH,
            "sensitivity": KnowledgeBase.Sensitivity.INTERNAL,
            "authority": "Acme Retail (fictional, sample fixture)",
            "external_id": kb_external_id,
            "created_by": user,
        },
    )
    if not created and kb_external_id and not knowledge_base.external_id:
        knowledge_base.external_id = kb_external_id
        knowledge_base.save(update_fields=["external_id", "updated_at"])

    tool_meta = DEMO_AGENT["tool"]
    tool, _ = Tool.objects.get_or_create(
        project=project,
        name=tool_meta["name"],
        defaults={
            "type": tool_meta["type"],
            "description": tool_meta["description"],
            "read_only": True,
            "has_side_effects": False,
            "external_network": False,
            "handles_sensitive_data": False,
            "external_id": tool_external_id,
            "created_by": user,
        },
    )
    if not tool.external_id and tool_external_id:
        tool.external_id = tool_external_id
        tool.save(update_fields=["external_id", "updated_at"])

    meta = DEMO_AGENT
    agent, created = Agent.objects.get_or_create(
        project=project,
        name=meta["name"],
        defaults={
            "description": meta["description"],
            "base_model": base_model,
            "system_prompt": meta["system_prompt"],
            "retrieval_settings": {
                "search_mode": "hybrid",
                "top_k": 5,
                "relevance_threshold": 0.2,
            },
            "capabilities": meta["capabilities"],
            "created_by": user,
        },
    )
    agent.knowledge_bases.set([knowledge_base])
    agent.tools.set([tool])
    agent.save()
    status["agent"] = "created" if created else "reused"
    return status


def backfill_demo_chat_resources(project, user) -> dict[str, int]:
    """Push the seeded demo agent, KB and tool to Open WebUI (chat startup).

    ``setup_local`` seeds the demo rows while Open WebUI is not up, so they are
    local-only (empty ``external_id``) and the OWUI-backed /agents/,
    /agents/knowledge/ and /agents/tools/ pages look empty. When the chat stack
    becomes ready, this completes the push, idempotently: rows already synced
    are skipped, a row whose earlier push partially failed is re-pushed (the
    pushes reuse existing OWUI entries by name/id, so nothing is duplicated),
    and each ``external_id`` is backfilled on success.

    Order matters: the agent is pushed last, because ``push_agent`` reads the
    KB/tool ``external_id``s to wire them into the agent's OWUI model. A demo
    agent that was already pushed before its KB/tool were is re-pushed (it is
    an update) so the links land.

    Never raises. Returns ``{"agents": n, "knowledge_bases": k, "tools": m}``
    (zeros when chat is disabled, nothing is missing, or Open WebUI is
    unreachable).
    """
    counts = {"agents": 0, "knowledge_bases": 0, "tools": 0}
    try:
        from chat import config as chat_config

        if not getattr(chat_config, "ENABLED", False):
            return counts
        from chat.api import ChatAPI, ChatAPIError
        from model_registry.models import Agent, KnowledgeBase, Tool

        kb = KnowledgeBase.objects.filter(
            project=project, name=DEMO_AGENT["knowledge_base"]["name"]
        ).first()
        tool = Tool.objects.filter(
            project=project, name=DEMO_AGENT["tool"]["name"]
        ).first()
        agent = Agent.objects.filter(
            project=project, name=DEMO_AGENT["name"]
        ).first()
        kb_missing = kb is not None and not kb.external_id
        tool_missing = tool is not None and not tool.external_id
        agent_missing = agent is not None and not agent.external_id
        # An existing external id only proves that the workspace model was
        # created once.  Its base model can still be stale after a Studio-side
        # agent re-point (for example, from OpenAI to a custom provider), so
        # the agent itself must be reconciled whenever chat becomes ready.
        if not (kb_missing or tool_missing or agent is not None):
            return counts

        from model_registry.services import sync_agent_to_openwebui

        api = ChatAPI.as_user(user)
        if kb_missing or tool_missing:
            # One push per call: it reuses existing OWUI entries by name/id, so
            # a partial earlier push completes here instead of duplicating.
            kb_external_id, tool_external_id = _push_to_openwebui(api, logger.info)
            if kb_missing and kb_external_id:
                kb.external_id = kb_external_id
                kb.save(update_fields=["external_id", "updated_at"])
                counts["knowledge_bases"] += 1
            if tool_missing and tool_external_id:
                tool.external_id = tool_external_id
                tool.save(update_fields=["external_id", "updated_at"])
                counts["tools"] += 1
        # Agent last: always reconcile it when present.  This repairs stale
        # base_model_id values as well as missing KB/tool links.
        if agent is not None:
            result = sync_agent_to_openwebui(agent, user)
            if result in {"created", "updated"}:
                agent.refresh_from_db()
                # Count only a genuine backfill (id empty → set). A re-wire of
                # an already-synced agent keeps its links current without being
                # reported as a new backfill.
                if agent_missing and agent.external_id:
                    counts["agents"] += 1
        return counts
    except ChatAPIError:
        logger.info("Demo chat backfill skipped (could not reach Open WebUI).")
        return counts
    except Exception as exc:  # noqa: BLE001 - startup must not fail on chat
        logger.info("Demo chat backfill skipped: %s", exc)
        return counts
