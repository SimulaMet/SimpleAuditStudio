# Open WebUI Resource Management — Design

**Date:** 2026-10-03
**Status:** Draft (pending user review)
**Owner:** (TBD)

## 1. Problem

Today, the resources an agent can use — **Knowledge Bases**, **Tools** (functions), and
**MCP Servers/Tools** — can only be created *inside* Open WebUI, then pulled into Studio
via the existing sync (`_sync_openwebui_resources`). There is no way to create or edit
them from Studio itself. Users who want to author a new knowledge base or tool must leave
Studio, open Open WebUI directly, and come back.

We want Studio to be the single place where a user creates and manages these resources,
with Open WebUI as the backing store (so the resources are immediately usable by the
chat/agent runtime that Open WebUI serves).

## 2. Goals

- Let a signed-in Studio user **create, edit, and delete** Knowledge Bases, Tools, and
  MCP Servers/Tools **from within Studio** — by embedding Open WebUI's native admin UI,
  scoped down to the relevant section.
- **Delegate the heavy lifting to Open WebUI:** document upload, chunking, and MCP
  configuration are handled by Open WebUI's own (polished) UI, not re-implemented in
  Studio.
- The existing **pull-sync** (Open WebUI → Studio models) keeps Studio's local models
  fresh so the resources appear in the agent pickers.
- Reuse the existing Open WebUI infrastructure (forward-auth proxy, `ChatAPI`, embedded
  process) — no new external services.
- Surface the resource manager as a **submenu under "Agents"** in the sidebar, at
  `/agents/resources/`.

## 3. Non-Goals (v1)

- Re-implementing Open WebUI's forms in Studio (that was Approach B; rejected in favor of
  delegating to the iframe).
- Studio **pushing** writes to Open WebUI — the iframe *is* the editor; Studio only reads
  (pull-sync).
- Fine-grained per-resource permissions beyond the existing project/workspace model.
- Managing models/providers (already handled by the Models page + `push_connections`).
- Managing chat settings, users, or other Open WebUI admin surfaces beyond the three
  resource types.

## 4. Background: what already exists

| Piece | Location | Role |
|---|---|---|
| Open WebUI process | `chat/proxy.py:start_open_webui` | Runs on `127.0.0.1:8080`, started by `dev_server --embedded` / `uvx` when `SIMPLEAUDIT_CHAT=embedded` |
| Forward-auth proxy | `chat/proxy.py` | Listens on `8801`; identifies the browser via `GET /chat/authz` and injects `X-Studio-*` headers; serves Studio's `embed.css` as Open WebUI's `/static/custom.css` |
| `ChatAPI` | `chat/api.py` | Per-user client. `as_user(user)` signs in with the user's identity. Generic `request(method, path, json)` can call any `/api/v1/*` endpoint. Already has `knowledge_bases()` (pull) |
| Pull-sync | `infra/ui.py:_sync_openwebui_resources` | Reads KBs + functions from Open WebUI, `update_or_create`s local `KnowledgeBase` / `Tool` rows |
| Local models | `model_registry/models.py` | `KnowledgeBase`, `Tool`, `MCPServer`, `MCPTool`, `RetrievalProfile`, `Agent` |
| Agent UI | `infra/ui.py:AgentDetailView`, `agents/agent_detail.html` | Create/edit agent with pickers for KBs/tools/MCP |

## 5. Design decision: restricted iframe (Approach A) — delegate to Open WebUI

**Decision (confirmed with user):** use the **restricted iframe**. Studio embeds Open
WebUI's native admin pages and masks everything except the target section. We do **not**
re-implement Open WebUI's forms in Studio.

### Why A (per user direction)

1. **Don't re-implement what Open WebUI already does well.** Open WebUI has polished,
   battle-tested UIs for exactly these resources — most importantly **document upload and
   chunking** for knowledge bases, and **MCP server configuration**. Rebuilding that in
   Studio forms (Approach B) would be a large effort to replicate functionality we'd
   rather inherit.
2. **Delegate, don't duplicate.** The iframe *is* Open WebUI's UI, so it stays in sync
   with Open WebUI's features for free. Studio's job is just to surface the right page,
   scoped down, and keep its local models in step (the pull-sync already does this).
3. **The masking mechanism already exists.** The proxy serves Studio's `embed.css` as
   Open WebUI's `/static/custom.css` on every page — that's how the chat page hides the
   sidebar. We extend this with a second, admin-oriented stylesheet.

### The known trade-off (accepted)

CSS masking is **fragile across Open WebUI upgrades**: if Open WebUI renames a DOM id or
class, a hidden element can silently reappear (the existing `embed.css` comments document
this exact risk). We accept this trade-off in exchange for not re-implementing the forms.
Mitigations:
- Keep the admin mask **additive and targeted** (hide known chrome, don't try to allowlist
  every pixel), so a missed element degrades gracefully rather than breaking the page.
- Add a **visual-regression note** in the chat/Open WebUI upgrade checklist: after bumping
  Open WebUI, load `/agents/resources/` and confirm the mask still hides the chrome.
- The mask lives in one file (`chat/embed_admin.css`) so it's a single place to fix.

### What Studio still does (the thin part)

- Serve the `/agents/resources/` page with the iframe + a slim Studio top bar (back link,
  section tabs, a "Sync to Studio" button).
- Point the iframe at the right Open WebUI admin page per section (knowledge / functions /
  MCP).
- Run the existing **pull-sync** so Studio's local `KnowledgeBase` / `Tool` / `MCPServer` /
  `MCPTool` models reflect what the user created in Open WebUI — this is what makes the
  resources appear in the agent pickers.
- Keep an **"Open in Open WebUI"** full-window link (same deep-link, no iframe) as a
  fallback if the mask ever breaks.

## 6. Architecture

```
Browser
  │  (signed in to Studio)
  ▼
Studio Django  ── new view: AgentResourcesView (serves /agents/resources/)
  │                    renders the iframe page + slim top bar + section tabs
  │
  │  iframe src = proxy origin + Open WebUI admin path (per section)
  ▼
Forward-auth proxy (8801)  ── identifies browser via /chat/authz, injects X-Studio-*
  │                            headers, and serves chat/embed_admin.css as
  │                            Open WebUI's /static/custom.css (the mask)
  ▼
Open WebUI (8080)  ── native admin pages: /knowledge, /functions, /mcp
  │   (user creates/edits/deletes resources HERE, in Open WebUI's own UI)
  │
  │  pull-sync (existing, ChatAPI.as_user)
  ▼
Studio local models  (KnowledgeBase / Tool / MCPServer / MCPTool)
  │
  ▼
AgentDetailView pickers  (unchanged — they already list these models)
```

Data flow:
- **User creates/edits a resource** *inside the embedded Open WebUI* (its native UI —
  upload, chunking, MCP config all handled by Open WebUI).
- **Studio picks it up** via the existing pull-sync (`_sync_openwebui_resources`), which
  runs on agent-page load and on the "Sync to Studio" button. This `update_or_create`s
  the local models so the resource appears in the agent pickers.
- **No push from Studio.** Studio never writes to Open WebUI for these resources — the
  iframe *is* the editor. This is the core simplification vs. Approach B.

## 7. Components

### 7.1 New view: `AgentResourcesView` (in `infra/ui.py`, next to the other agent views)

A `ProjectMixin, TemplateView` that serves `/agents/resources/`. It:
- 404s (or shows a "Chat is not enabled" state) when `chat_enabled()` is false — same
  guard the chat views use.
- Requires an authenticated user with an active project (the `ProjectMixin` already does
  this).
- Renders `agents/resources.html` with:
  - the **section** (default `knowledge`; also `functions`, `mcp`) — from `?section=` or
    the URL.
  - the **iframe src**: `config.public_url(request)` + the Open WebUI admin path for that
    section (see §7.4 for the paths).
  - the **local model counts** for the section (so the top bar can show "3 knowledge
    bases synced"), by querying the project's local models.
- Runs `_sync_openwebui_resources(project, user)` on GET (same as `AgentDetailView`
  already does), so the counts and agent pickers are fresh.

### 7.2 The admin mask: `chat/embed_admin.css` (new file)

A second stylesheet, served by the proxy **in place of** Open WebUI's `/static/custom.css`
*when the request is for the admin embed*. It hides Open WebUI's app chrome (top nav,
settings sidebar, model picker, etc.) and leaves only the target section's content area.

**How the proxy picks which CSS to serve.** Today `_serve_embed_css` always returns
`chat/embed.css`. We extend it to choose based on the request: if the upstream request is
for an admin path (or carries a marker Studio adds to the iframe URL, e.g.
`?__studio_admin=1`), serve `embed_admin.css`; otherwise serve `embed.css` (chat). The
marker-query approach is more robust than path-matching because it doesn't depend on
Open WebUI's route strings. The proxy strips the marker before forwarding to upstream.

> **Implementation note:** the exact selectors to hide depend on Open WebUI's current DOM.
> This must be built and verified against the live instance (port 8080) — it is the main
> implementation-time work item, and the part most likely to need re-tuning after an
> Open WebUI upgrade.

### 7.3 Local model changes (none expected)

The local models already have `external_id` (the pull-sync join key). Since Studio no
longer pushes, no new fields are needed. If the pull-sync doesn't yet cover MCP servers
or tools fully, extend `_sync_openwebui_resources` to map them — but that's a sync
completeness fix, not a model change.

### 7.4 Open WebUI admin paths per section

The iframe points at Open WebUI's native admin routes. Exact paths to be confirmed against
the live instance; expected to be of the form:
- Knowledge: `/settings/knowledge` (or `/knowledge`)
- Functions/Tools: `/settings/functions` (or `/functions`)
- MCP: `/settings/mcp` (or `/mcp`)

These are confirmed during implementation by loading the running Open WebUI and reading
its routes. The view centralizes them in one dict so they're trivial to adjust.

### 7.5 "Open in Open WebUI" full-window fallback

A top-bar link that opens the same deep-link **without** the iframe (a normal navigation
to the proxy origin). If the mask ever breaks on an upgrade, the user still has full
access to Open WebUI's native UI. No new infra.

### 7.6 Navigation: "Resources" as a submenu under Agents

The sidebar (`templates/partials/sidebar.html` + `_NAV` in `infra/context_processors.py`)
is currently a flat list. To nest "Resources" under "Agents":
- Add a `resources` entry to `_NAV` with prefix `("/agents/resources/",)`.
- In `sidebar.html`, render items whose prefix starts with `/agents/` (other than the
  base `/agents/`) as an indented sub-item under the active "Agents" entry.
- Active-state: the existing longest-prefix `score()` already handles highlighting the
  right item; the sub-item highlights when on `/agents/resources/`.

This keeps all agent-related surfaces (list, detail, resources) grouped under one
"Agents" nav entry, as requested.

## 8. Error handling

- **Chat disabled / Open WebUI unreachable:** the resource page shows a clear "Chat is not
  enabled — resources can't be managed here" state (mirrors the existing
  `_sync_openwebui_resources` `None` return and the `/agents/sync/` 400). The iframe is
  not rendered.
- **Mask breaks on an Open WebUI upgrade:** the page still loads (the iframe shows the
  full Open WebUI admin page, just with some chrome visible). The "Open in Open WebUI"
  full-window link is always available as a fallback. This is a graceful degradation, not
  a failure — the user can still manage resources.
- **Identity:** the iframe request goes through the forward-auth proxy, which identifies
  the browser via `/chat/authz` and injects `X-Studio-*` headers. Open WebUI applies the
  user's own permissions — a user who can't create a KB in Open WebUI sees the same
  restriction inside the iframe as in the native UI.
- **Pull-sync failure:** if `_sync_openwebui_resources` can't reach Open WebUI, the local
  counts are stale but the page still renders (the iframe is independent of the sync).

## 9. Testing

- **View test (`infra/tests/`):** `AgentResourcesView` returns 200 with the iframe for an
  authenticated user with a project; returns the "chat not enabled" state when
  `SIMPLEAUDIT_CHAT` is off; 404/redirects for an unauthenticated user.
- **Proxy CSS-selection test (`chat/tests/`):** assert `_serve_embed_css` returns
  `embed_admin.css` for a request carrying the admin marker and `embed.css` otherwise,
  and that the marker is stripped before forwarding upstream.
- **Pull-sync tests** remain green (no behavior change to `_sync_openwebui_resources`).
- **Manual verification (documented, not automated):** after implementation, load
  `/agents/resources/` in a browser and confirm (a) the mask hides Open WebUI's chrome,
  (b) creating a KB/tool/MCP in the iframe appears in the agent pickers after sync, and
  (c) the "Open in Open WebUI" link works. This manual step is the practical check for
  the CSS mask, which is hard to unit-test.

## 10. Open Questions (to resolve during implementation)

1. **Exact Open WebUI admin routes** for knowledge / functions / MCP (expected
   `/settings/knowledge`, `/settings/functions`, `/settings/mcp` — confirm against the
   live instance on port 8080).
2. **Exact DOM selectors** for the admin mask (`embed_admin.css`) — build and verify
   against the live instance; this is the main work item.
3. **MCP route availability:** confirm the running Open WebUI version exposes an MCP
   admin page. If it doesn't, the MCP section links out full-window instead of embedding.
4. **Marker mechanism:** confirm the `?__studio_admin=1` query marker is the cleanest way
   to tell the proxy which CSS to serve (vs. path-matching), and that Open WebUI ignores
   unknown query params on its admin routes.

## 11. Phasing

- **Phase 1 — Page + Knowledge section:** `AgentResourcesView` + `agents/resources.html`
  (iframe + top bar + tabs) + `embed_admin.css` mask for the knowledge page + nav
  submenu. End-to-end: create a KB in the iframe → sync → see it in agent pickers.
- **Phase 2 — Tools section:** point the iframe at the functions route; extend the mask;
  verify tool sync.
- **Phase 3 — MCP section:** point the iframe at the MCP route; extend the mask; verify
  MCP sync (or full-window link if no embeddable MCP page).
- **Phase 4 — Polish:** "Open in Open WebUI" fallback link, stale-count handling,
  upgrade-checklist note for the mask.

Each phase is independently shippable. Phase 1 is the critical path and proves the whole
iframe + mask + sync loop before the other sections are added.
