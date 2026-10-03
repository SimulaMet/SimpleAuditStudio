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
  MCP Servers/Tools from within Studio.
- Changes made in Studio are **pushed to Open WebUI** so the runtime sees them.
- The existing **pull-sync** (Open WebUI → Studio models) continues to keep Studio's
  local models fresh and is the source of truth for what an agent *can* reference.
- Reuse the existing Open WebUI infrastructure (forward-auth proxy, `ChatAPI`, embedded
  process) — no new external services.
- Stay robust across Open WebUI upgrades.

## 3. Non-Goals (v1)

- Replacing Open WebUI's own admin UI. Open WebUI remains the authoritative store.
- Fine-grained per-resource permissions beyond the existing project/workspace model.
- Managing models/providers (already handled by the Models page + `push_connections`).
- Managing chat settings, users, or other Open WebUI admin surfaces.

## 4. Background: what already exists

| Piece | Location | Role |
|---|---|---|
| Open WebUI process | `chat/proxy.py:start_open_webui` | Runs on `127.0.0.1:8080`, started by `dev_server --embedded` / `uvx` when `SIMPLEAUDIT_CHAT=embedded` |
| Forward-auth proxy | `chat/proxy.py` | Listens on `8801`; identifies the browser via `GET /chat/authz` and injects `X-Studio-*` headers; serves Studio's `embed.css` as Open WebUI's `/static/custom.css` |
| `ChatAPI` | `chat/api.py` | Per-user client. `as_user(user)` signs in with the user's identity. Generic `request(method, path, json)` can call any `/api/v1/*` endpoint. Already has `knowledge_bases()` (pull) |
| Pull-sync | `infra/ui.py:_sync_openwebui_resources` | Reads KBs + functions from Open WebUI, `update_or_create`s local `KnowledgeBase` / `Tool` rows |
| Local models | `model_registry/models.py` | `KnowledgeBase`, `Tool`, `MCPServer`, `MCPTool`, `RetrievalProfile`, `Agent` |
| Agent UI | `infra/ui.py:AgentDetailView`, `agents/agent_detail.html` | Create/edit agent with pickers for KBs/tools/MCP |

## 5. Design decision: native Studio forms (Approach B)

> **Note on the original proposal.** The initial idea was to embed Open WebUI's admin
> pages in an iframe and mask everything except the target section (Approach A). That is
> technically feasible — the proxy already serves Studio's `embed.css` as Open WebUI's
> `/static/custom.css`, which is how the chat page hides the sidebar. But the existing
> `embed.css` comments document that this CSS-masking is fragile across Open WebUI
> upgrades, and the admin pages have *more* chrome to hide than the chat page. This spec
> therefore recommends native Studio forms (Approach B) as the primary path, and keeps the
> iframe only as an "Open in Open WebUI" escape hatch. **If the native Open WebUI admin UI
> is a hard requirement** (e.g. its file-upload UX), flip to Approach A — the push/pull API
> layer in §7.1 is shared by both, so only the front-end changes.

### Options considered

- **A. Restricted iframe** — embed Open WebUI's admin pages in an iframe and use a second
  `embed.css` variant to mask all chrome except the target section.
- **B. Native Studio forms** — Studio builds its own create/edit forms that call Open
  WebUI's REST API via `ChatAPI` under the hood. *(chosen)*
- **C. Hybrid** — native forms for create/edit, iframe only for a "preview in Open WebUI"
  escape hatch.

### Why B

1. **Robustness.** The existing `embed.css` comments explicitly warn that CSS masking is
   fragile: Open WebUI's DOM ids/classes change on upgrades, and a renamed id silently
   re-exposes hidden UI. A second, more aggressive mask for the admin pages would be even
   more fragile (admin pages have more chrome to hide). Native forms have no such coupling.
2. **Consistency.** The `/agents/` pages already use Studio's design language. Native
   forms match it; an embedded Open WebUI admin panel would look like a different app.
3. **The hard part already exists.** `ChatAPI.request()` can POST/PUT/DELETE to any
   `/api/v1/*` endpoint, and the pull-sync already maps Open WebUI items to local models.
   We are adding the *push* direction and the forms on top of proven plumbing.
4. **Scope control.** Native forms let us show exactly the fields we want and validate
   them server-side, rather than trusting whatever Open WebUI's form accepts.

### The iframe, kept as an escape hatch

We do **not** drop the iframe entirely. We keep a small **"Open in Open WebUI"** link on
each resource (and a page-level link) that deep-links to the relevant Open WebUI admin
page through the existing proxy. This covers the cases where Open WebUI's native UI offers
something Studio's form doesn't (e.g. dragging files into a knowledge base), without making
the fragile mask the primary path.

> **Assumption (reversible):** if the user specifically wants Open WebUI's *native* admin
> UI as the primary surface (e.g. because file-upload UX matters more than robustness),
> this flips to Approach A. The spec is written so the push/pull API layer is shared by
> both — only the front-end differs.

## 6. Architecture

```
Browser
  │  (signed in to Studio)
  ▼
Studio Django  ── new views: ResourceCreateView / ResourceEditView / ResourceDeleteView
  │                    (one set per resource type, or a generic CRUD + serializer)
  │
  │  ChatAPI.as_user(request.user)
  ▼
Forward-auth proxy (8801)  ── injects X-Studio-* identity headers
  │
  ▼
Open WebUI (8080)  ── /api/v1/knowledge/, /api/v1/functions/, /api/v1/mcp/
  │
  ▼  (pull-sync, existing)
Studio local models  (KnowledgeBase / Tool / MCPServer / MCPTool)
  │
  ▼
AgentDetailView pickers  (unchanged — they already list these models)
```

Data flow:
- **Create/edit in Studio** → view calls `ChatAPI` (push) → Open WebUI stores it → view
  also `update_or_create`s the local model (or re-runs pull-sync) → agent pickers see it.
- **Created directly in Open WebUI** → existing pull-sync picks it up on next agent-page
  load (or a manual "Sync" button, which already exists at `/agents/sync/`).

## 7. Components

### 7.1 `ChatAPI` push methods (new, in `chat/api.py`)

Add thin wrappers over `request()` for the write operations, mirroring the existing
read methods:

- `create_knowledge(name, description, ...)` → `POST /api/v1/knowledge/`
- `update_knowledge(kb_id, ...)` → `PUT /api/v1/knowledge/{id}/`
- `delete_knowledge(kb_id)` → `DELETE /api/v1/knowledge/{id}/`
- `create_function(name, description, parameters, code)` → `POST /api/v1/functions/`
- `update_function(fn_id, ...)` / `delete_function(fn_id)`
- `create_mcp_server(...)` / `update_mcp_server(...)` / `delete_mcp_server(...)`
  (exact Open WebUI MCP endpoints to be confirmed against the running instance — see
  Open Questions)

Each returns the Open WebUI item dict; the view maps it to the local model. Errors raise
`ChatAPIError`, which the view turns into a user-facing message (the resource was *not*
created/changed in Open WebUI).

> **Note:** the exact Open WebUI REST paths and payload shapes for functions and MCP
> servers must be verified against the live instance (port 8080) before implementation.
> Knowledge-base paths are already known (`/api/v1/knowledge/`). This is the main
> implementation-time unknown and is isolated to `chat/api.py`.

### 7.2 Local model changes (likely none or minimal)

The local models already have `external_id` (used by pull-sync as the join key to Open
WebUI). Push operations should set `external_id` to the id Open WebUI returns, so the
next pull-sync reconciles correctly. If a model is missing a field the form needs (e.g. a
tool's `code`/`parameters`), add it — but prefer reusing existing fields.

### 7.3 New views (in `model_registry/agent_views.py` or a new `resource_views.py`)

A small set of DRF API endpoints (matching the existing `retrieval_profiles` style) plus
the HTML views/templates for the forms. Two sub-options:

- **B1 (recommended): API + thin HTML forms.** Add DRF endpoints
  (`POST/PUT/DELETE /api/resources/knowledge/`, etc.) that do the push + local
  `update_or_create`. The HTML page calls them via `fetch`, exactly like the existing
  `/agents/sync/` button. Keeps the push logic testable and reusable.
- **B2: Server-rendered forms.** Classic Django form POSTs. Simpler, less JS, but the
  push logic lives in the view.

B1 is preferred for consistency with the existing `/agents/sync/` JSON pattern.

### 7.4 New UI surface

A **"Resources"** section under `/agents/` (e.g. `/agents/resources/`) with three tabs or
sub-sections: Knowledge Bases, Tools, MCP Servers. Each lists the local models (already
queryable by project) with Create / Edit / Delete / "Open in Open WebUI" actions. This
reuses the `agents/` templates and the `ProjectMixin` pattern.

### 7.5 "Open in Open WebUI" escape hatch

A link per resource that points at the proxy origin (via `config.public_url(request)`)
deep-linked to the relevant Open WebUI admin page. No new infra — the proxy already
handles auth and CSS.

## 8. Error handling

- **Open WebUI unreachable / chat disabled:** the resource page shows a clear "Chat is not
  enabled — resources can't be managed" state (mirrors the existing `_sync_openwebui_resources`
  `None` return and the `/agents/sync/` 400).
- **Push fails (4xx/5xx from Open WebUI):** surface the `ChatAPIError` message; do **not**
  update the local model (so Studio doesn't claim a resource exists that Open WebUI
  rejected).
- **Identity:** all calls go through `ChatAPI.as_user(request.user)`, so the user's own
  Open WebUI permissions apply. A user without permission to create a KB in Open WebUI
  gets the same 403 they'd get in the native UI.

## 9. Testing

- **Unit (`chat/tests/`):** mock `ChatAPI.request` and assert the push methods hit the
  right path/verb/payload and map the response to the local model.
- **View tests (`model_registry/tests/` or `infra/tests/`):** create/edit/delete a
  resource via the API endpoint with a mocked `ChatAPI`; assert the local model is
  `update_or_create`d with the returned `external_id`, and that a failed push leaves the
  local model unchanged.
- **Disabled-chat path:** assert the resource endpoints return the "chat not enabled"
  state when `SIMPLEAUDIT_CHAT` is off.
- **Existing pull-sync tests** remain green (no behavior change).

## 10. Open Questions (to resolve during/after review)

1. **Exact Open WebUI REST endpoints + payload shapes** for functions and MCP servers
   (knowledge bases are known). Verify against the live instance on port 8080.
2. **MCP scope:** does v1 need full MCP server *and* tool management, or just listing +
   linking? (MCP is the least certain of the three.)
3. **File upload for knowledge bases:** Open WebUI's KB creation involves uploading
   documents. Does v1 support upload from Studio's form, or only create an empty KB +
   "Open in Open WebUI" to add files?
4. **Naming/URL:** confirm `/agents/resources/` vs. a top-level `/resources/`.

## 11. Phasing

- **Phase 1:** `ChatAPI` push methods + Knowledge Base create/edit/delete (end-to-end,
  since KB paths are already known). Resource page shell with the KB tab.
- **Phase 2:** Tools (functions) create/edit/delete.
- **Phase 3:** MCP Servers/Tools (after endpoint shapes are confirmed).
- **Phase 4:** "Open in Open WebUI" escape-hatch links + polish.

Each phase is independently shippable and testable.
