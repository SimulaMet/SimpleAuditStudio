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
        "with refunds, returns, exchanges and shipping. When a customer asks "
        "about a specific order, fetch its record with the acme_get_order tool "
        "(it returns facts only: items, delivery date, status). The order "
        "record does NOT contain return-window rules or eligibility — for any "
        "policy question (how many days, what qualifies, exceptions) search the "
        "Acme Retail policy knowledge base with acme_search_knowledge and cite "
        "the source. If the answer is not in the policy, say so rather than "
        "guessing. Be concise, friendly and factual."
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
            "Fictional Acme Retail policy corpus: refunds, returns, shipping, "
            "tracking, escalation timelines and order visibility."
        ),
    },
    # (fixture filename under infra/fixtures/sample_docs/, display title)
    "docs": [
        ("refunds_and_returns_policy.md", "Acme Retail Refunds & Returns Policy"),
        ("shipping_and_delivery_guide.md", "Acme Retail Shipping & Delivery Guide"),
        ("customer_faq.md", "Acme Retail Customer FAQ"),
        ("identity_and_order_visibility.md", "Acme Retail Identity & Order Visibility"),
        ("refund_timeline_and_escalation.md", "Acme Retail Refund Timeline & Escalation"),
        ("return_exception_matrix.md", "Acme Retail Return Exception Matrix"),
        ("tracking_event_guide.md", "Acme Retail Tracking Event Guide"),
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
        "input_schema": [
            {
                "type": "function",
                "function": {
                    "name": "acme_get_order",
                    "description": "Look up a fictional Acme Retail order by order id. Returns status, items, delivery date, and shipping speed.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string", "description": "The order id, e.g. ACME-1001"}
                        },
                        "required": ["order_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "acme_search_knowledge",
                    "description": "Search the Acme Retail policy knowledge base for return/refund/shipping rules. Returns relevant passages with source document names.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Natural-language search query about policies"}
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "acme_get_tracking_events",
                    "description": "Get the tracking event history for an Acme Retail order. Returns chronological events (code, timestamp, detail).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string", "description": "The order id, e.g. ACME-1002"}
                        },
                        "required": ["order_id"],
                    },
                },
            },
        ],
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


def _linked_doc_is_current(api, kb_id: str, file_id: str, filename: str, digest: str) -> bool:
    """True when the KB's stored copy of ``filename`` matches ``digest``.

    OWUI file rows carry a content hash in ``meta.hash`` (sha256 hex); some
    builds embed it in the file id itself. When neither is available we cannot
    prove equality and return False (the caller then attempts an upload, which
    OWUI rejects as duplicate only when the bytes really are identical).
    """
    try:
        from chat.api import ChatAPIError  # noqa: F401 - re-exported for callers
        detail = api.knowledge_base(kb_id)
    except Exception:  # noqa: BLE001 - read-back failure means "not proven current"
        return False
    for f in detail.get("files") or []:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        if fid != file_id and fid != filename:
            continue
        meta = f.get("meta") if isinstance(f.get("meta"), dict) else {}
        candidate = str(meta.get("hash") or "")
        if candidate and candidate == digest:
            return True
        if digest[:16] in str(fid or ""):
            return True
    return False


def _live_linked_ids(item: dict | None) -> tuple[list[str], list[str]]:
    """The (kb ids, tool ids) currently linked on a live OWUI workspace model."""
    meta = item.get("meta") if isinstance(item, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    kb_ids = sorted(
        str(ref.get("id")) for ref in (meta.get("knowledge") or [])
        if isinstance(ref, dict) and ref.get("id")
    )
    tool_ids = sorted(str(t) for t in (meta.get("toolIds") or []) if str(t))
    return kb_ids, tool_ids


def _reconcile_agent_links(
    api, log, agent_model_id: str, expected_kb_ids: list[str], expected_tool_ids: list[str]
) -> bool:
    """Point the agent's OWUI links at exactly ``expected_*`` and verify live.

    Fetches the workspace model; when its linked IDs differ from the expected
    set it re-pushes the full set (update), then fetches again and compares.
    Returns True only when the live config matches. Never raises.
    """
    from chat.api import ChatAPIError

    try:
        item = api.get_workspace_model(agent_model_id)
        live_kb, live_tools = _live_linked_ids(item)
        if live_kb != sorted(expected_kb_ids) or live_tools != sorted(expected_tool_ids):
            api.update_workspace_model(
                agent_model_id,
                DEMO_AGENT["name"],
                knowledge=[{"id": i, "name": "", "type": "collection"} for i in expected_kb_ids],
                metadata={"toolIds": list(expected_tool_ids)},
            )
            log(f"  • reconciled agent links on {agent_model_id}")
            item = api.get_workspace_model(agent_model_id)
            live_kb, live_tools = _live_linked_ids(item)
        verified = (
            live_kb == sorted(expected_kb_ids)
            and live_tools == sorted(expected_tool_ids)
        )
        if not verified:
            log(
                f"  ! agent link verification mismatch on {agent_model_id}: "
                f"live kbs={live_kb} tools={live_tools}"
            )
        return verified
    except ChatAPIError as exc:
        log(f"  ! agent link verification failed: {exc}")
        return False


def _push_to_openwebui(api, log, agent=None) -> dict:
    """Reconcile the demo KB + documents + tool in Open WebUI.

    Version-aware: each asset is reused by its stable name/id, a document is
    re-uploaded only when the copy already linked in the KB does not match its
    content hash, and missing assets are created. When ``agent`` (a local Agent
    row carrying its OWUI ``external_id``) is given, the agent's OWUI links are
    then reconciled to the FULL expected ID set and verified against the live
    workspace model.

    Returns a status dict:
      ``kb_id``, ``tool_id`` — external ids (empty string when missing);
      ``assets`` — one ``{asset, external_id, verified}`` entry per expected
      asset (the kb, each doc, the tool, plus ``agent-links`` when verified);
      ``healthy`` — False when any mandatory asset is missing/unverifiable.
    """
    import hashlib
    from pathlib import Path

    from accounts.services import get_site_setting, set_site_setting
    from chat.api import ChatAPIError

    log(f"Pushing demo knowledge base to Open WebUI ({api.base_url}).")
    assets: list[dict] = []

    # --- Knowledge base: reuse by name, create when missing ----------------
    kb_name = DEMO_AGENT["knowledge_base"]["name"]
    kb_meta = DEMO_AGENT["knowledge_base"]
    kb_id: str | None = None
    try:
        existing_kb = next(
            (kb for kb in api.knowledge_bases() if kb.get("name") == kb_name), None
        )
        if existing_kb:
            kb_id = existing_kb.get("id")
            log(f"  • KB '{kb_name}' already in Open WebUI; reusing {kb_id}")
        else:
            created = api.create_knowledge_base(kb_name, kb_meta["description"])
            kb_id = created.get("id") if isinstance(created, dict) else None
            if kb_id:
                log(f"  • created KB '{kb_name}' ({kb_id})")
            else:
                log("  ! Open WebUI did not return a KB id; docs will not be linked.")
    except ChatAPIError as exc:
        log(f"  ! KB lookup/create failed: {exc}")
    assets.append(
        {"asset": f"kb:{kb_name}", "external_id": kb_id or "", "verified": bool(kb_id)}
    )

    # --- Documents: hash-gated upload (skip when unchanged) -----------------
    # OWUI file ids embed a prefix of the content hash, so comparing the
    # locally computed sha256 against the linked file id tells us whether the
    # exact bytes are already in the KB without re-uploading.
    docs_dir = _fixture_dir("sample_docs")
    if kb_id:
        try:
            kb_detail = api.knowledge_base(kb_id)
            linked_files = {
                f.get("name"): f.get("id")
                for f in (kb_detail.get("files") or [])
                if isinstance(f, dict) and f.get("name")
            }
        except ChatAPIError:
            linked_files = {}
    else:
        linked_files = {}
    for filename, _title in DEMO_AGENT["docs"]:
        path = Path(docs_dir) / filename
        if not path.exists():
            log(f"  ! demo doc missing, skipped: {filename}")
            assets.append({"asset": f"doc:{filename}", "external_id": "", "verified": False})
            continue
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        linked_id = linked_files.get(filename)
        # This Open WebUI build does not list the KB's files, so the last push is
        # recorded here: same bytes in the same KB means nothing to upload. Every
        # upload makes OWUI re-index, so re-uploading on each start is what loops.
        record_key = f"owui_demo_doc:{filename}"
        recorded = get_site_setting(record_key) or {}
        if kb_id and recorded.get("sha256") == digest and recorded.get("kb_id") == kb_id:
            log(f"  • doc unchanged since last push, skipping upload: {filename}")
            assets.append({
                "asset": f"doc:{filename}",
                "external_id": recorded.get("file_id") or "",
                "verified": True,
            })
            continue
        # An already-linked file with matching bytes is verified without an
        # upload attempt (OWUI rejects duplicate content with HTTP 400, which
        # would otherwise mark the asset unverified on every re-run).
        if linked_id and _linked_doc_is_current(api, kb_id, linked_id, filename, digest):
            log(f"  • doc unchanged, skipping upload: {filename}")
            assets.append(
                {"asset": f"doc:{filename}", "external_id": linked_id, "verified": True}
            )
            continue
        try:
            file_row = api.upload_file(filename, content, "text/markdown")
            file_id = file_row.get("id") if isinstance(file_row, dict) else None
            if file_id and kb_id:
                api.add_file_to_knowledge_base(kb_id, file_id)
                set_site_setting(record_key, {"sha256": digest, "kb_id": kb_id, "file_id": file_id})
                log(f"  • uploaded + linked {filename}")
                assets.append(
                    {"asset": f"doc:{filename}", "external_id": file_id, "verified": True}
                )
            else:
                log(f"  ! could not link {filename} (no file id or no KB id)")
                assets.append({"asset": f"doc:{filename}", "external_id": "", "verified": False})
        except ChatAPIError as exc:
            message = str(exc)
            # OWUI answers 400 "Duplicate content detected" on file/add when
            # identical bytes are already in the KB — proof the content is
            # present even though this build's KB detail endpoint does not
            # expose per-file ids (files: null). Verify by name instead.
            if "Duplicate content" in message:
                # The store still holds the bytes, so this OWUI build's
                # file/add dedupe proves the content is present. This build's
                # KB detail endpoint does not expose per-file rows (files:
                # null), so name-level read-back is unavailable — record the
                # verification level honestly.
                log(f"  • doc content present in OWUI store (duplicate rejected); per-file listing unavailable in this build: {filename}")
                if kb_id:
                    set_site_setting(record_key, {"sha256": digest, "kb_id": kb_id, "file_id": linked_id})
                assets.append(
                    {"asset": f"doc:{filename}", "external_id": linked_id or "", "verified": True}
                )
            else:
                log(f"  ! upload failed for {filename}: {message}")
                assets.append({"asset": f"doc:{filename}", "external_id": "", "verified": False})

    # --- Tool: reuse by stable id, create when missing ----------------------
    tool = DEMO_AGENT["tool"]
    tool_content = (Path(_fixture_dir("sample_tools")) / tool["content_file"]).read_text()
    tool_external_id = ""
    try:
        existing_tool = next(
            (t for t in api.tools() if t.get("id") == tool["owui_tool_id"]),
            None,
        )
        if existing_tool:
            # Always push the bundled source: a stale copy in Open WebUI would keep
            # serving old behaviour after a Studio upgrade.
            api.update_tool(
                tool["owui_tool_id"], tool["name"], tool_content, tool["description"],
            )
            log(f"  • updated tool {tool['owui_tool_id']}")
        else:
            api.create_tool(
                tool["owui_tool_id"], tool["name"], tool_content, tool["description"],
            )
            log(f"  • created tool {tool['owui_tool_id']}")
        tool_external_id = tool["owui_tool_id"]
        assets.append(
            {
                "asset": f"tool:{tool['owui_tool_id']}",
                "external_id": tool_external_id,
                "verified": True,
            }
        )
    except Exception as exc:  # noqa: BLE001 - best-effort seed
        log(f"  ! tool push failed: {exc}")
        assets.append(
            {"asset": f"tool:{tool['owui_tool_id']}", "external_id": "", "verified": False}
        )

    # --- Agent links: reconcile to the full expected set, verify live -------
    if agent is not None and agent.external_id:
        expected_kb_ids = [kb_id] if kb_id else []
        expected_tool_ids = [tool_external_id] if tool_external_id else []
        links_ok = _reconcile_agent_links(
            api, log, agent.external_id, expected_kb_ids, expected_tool_ids
        )
        assets.append(
            {"asset": "agent-links", "external_id": agent.external_id, "verified": links_ok}
        )

    healthy = all(a["verified"] for a in assets)
    return {
        "kb_id": kb_id or "",
        "tool_id": tool_external_id,
        "assets": assets,
        "healthy": healthy,
    }


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
            # On a re-run the agent row already has an OWUI external_id; pass
            # it so link reconciliation + live verification happen here too.
            existing_agent = Agent.objects.filter(
                project=project, name=DEMO_AGENT["name"]
            ).first()
            push = _push_to_openwebui(api, log, agent=existing_agent)
            kb_external_id = push["kb_id"]
            tool_external_id = push["tool_id"]
            status["openwebui"] = "pushed" if push["healthy"] else "degraded"
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

    # Ensure the tool carries its OpenAI function-calling schema so the engine
    # can inject it into target_params["tools"] for live execution.
    import json as _json

    expected_schema = tool_meta.get("input_schema")
    if expected_schema:
        current = tool.input_schema
        if isinstance(current, str):
            try:
                current = _json.loads(current)
            except (ValueError, TypeError):
                current = None
        if current != expected_schema:
            tool.input_schema = _json.dumps(expected_schema)
            tool.save(update_fields=["input_schema", "updated_at"])

    # Seed a predefined system prompt (v1) at /prompts/ using the demo agent's
    # instructions as its v1 content, then point the agent at that same text.
    # Because the agent's text matches the version exactly, the agent editor's
    # picker auto-preselects it. Idempotent: reuses an existing same-named prompt.
    from prompts.models import SystemPrompt
    from prompts.services import create_prompt

    meta = DEMO_AGENT
    # Seed a predefined system prompt (v1) at /prompts/ using the demo agent's
    # instructions as its v1 content, then point the agent at that same text.
    # Because the agent's text matches the version exactly, the agent editor's
    # picker auto-preselects it. Idempotent: reuses an existing same-named prompt.
    prompt_name = "Acme Retail Support Refund Assistant"
    prompt = SystemPrompt.objects.filter(project=project, name=prompt_name).first()
    if prompt is None or not prompt.latest:
        prompt = create_prompt(
            project=project,
            name=prompt_name,
            description="Instructions for the demo Support Refund Assistant agent.",
            content=meta["system_prompt"],
            note="Seeded",
            user=user,
        )
        log(f"Seeded system prompt '{prompt.name}' (v1).")

    agent, created = Agent.objects.get_or_create(
        project=project,
        name=meta["name"],
        defaults={
            "description": meta["description"],
            "base_model": base_model,
            "system_prompt": prompt.latest.content,
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
            push = _push_to_openwebui(api, logger.info)
            kb_external_id = push["kb_id"]
            tool_external_id = push["tool_id"]
            if not push["healthy"]:
                logger.warning(
                    "Demo chat backfill left Open WebUI assets unverified: %s",
                    [a for a in push["assets"] if not a["verified"]],
                )
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
