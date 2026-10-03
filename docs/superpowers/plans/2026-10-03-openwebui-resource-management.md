# Open WebUI Resource Management (Restricted Iframe) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a signed-in Studio user create/edit/delete Knowledge Bases, Tools, and MCP Servers from within Studio by embedding Open WebUI's native workspace admin UI in an iframe (masked to the target section), with the existing pull-sync keeping Studio's local models fresh.

**Architecture:** A new `AgentResourcesView` serves `/agents/resources/` with an iframe pointing at the chat proxy. The proxy already hijacks Open WebUI's `/static/custom.css` request and serves Studio's `chat/embed.css`; we extend it to serve a new `chat/embed_admin.css` (which hides Open WebUI's workspace tab nav and chrome, keeping only `#workspace-container`) when the request carries a `?__studio_admin=1` marker. Studio never pushes writes — the iframe *is* the editor; the existing `_sync_openwebui_resources` pull keeps local `KnowledgeBase`/`Tool` rows fresh so they appear in the agent pickers.

**Tech Stack:** Django (views, URLs, templates), the existing `http.server`-based forward-auth proxy (`chat/proxy.py`), plain CSS, pytest + `httpx` (proxy tests), Django `TestCase`/`APIClient` (view tests).

**Spec:** `docs/superpowers/specs/2026-10-03-openwebui-resource-management-design.md`

## Global Constraints

- **Verified Open WebUI routes (this version):** the workspace admin lives under `/workspace/*`, NOT `/settings/*`. Confirmed against the live instance on `127.0.0.1:8080`:
  - Knowledge Bases → `/workspace/knowledge`
  - Tools (functions) → `/workspace/tools`
  - MCP → **no dedicated workspace page in this version.** The Tools page and the tool-create form (`/workspace/tools/create`) expose no MCP option, and `/api/v1/mcp/*` returns the SPA 404 fallback (not a real API). MCP is therefore surfaced as a note/link in the Tools section, not a separate embeddable page.
- **Verified DOM hooks (this version):**
  - The workspace content container has the stable id `#workspace-container` — **keep** it.
  - The workspace tab nav (Models/Knowledge/Prompts/Skills/Tools) is a `<nav>` with no id/aria-label, only Tailwind classes; it is the first child of the first child of `main#main-content`. Hide it structurally: `main#main-content > div > nav`.
  - The "Open Sidebar" button is `#sidebar-toggle-button` — already hidden by `chat/embed.css`.
  - There is **no chat sidebar** on workspace pages, so `embed.css`'s sidebar rules are inert there.
- **No push from Studio.** Studio only reads (pull-sync). Do not add any write path to Open WebUI for these resources.
- **Additive CSS only.** `embed_admin.css` must only use `display: none !important` / layout rules that degrade gracefully if a selector drifts — never a rule that breaks the page.
- **DRY:** reuse `chat/embed.css`'s existing hooks; `embed_admin.css` is served *instead of* `embed.css` for admin requests, so it must re-include the hooks that still apply (the sidebar toggle) or the two files must be composed. See Task 2.
- **Locale-fragile hooks** (English `aria-label`s) are acceptable and already used in `embed.css`; prefer stable ids/structural selectors where available.
- **Tests:** proxy behavior tested with the existing stub-server harness in `chat/tests/test_proxy.py`; view behavior with Django `TestCase` + `APIClient`. Run with `uv run pytest <path> -q`.

---

### Task 1: The admin mask stylesheet `chat/embed_admin.css`

**Files:**
- Create: `chat/embed_admin.css`

**Interfaces:**
- Consumes: nothing (static asset).
- Produces: a CSS file served by the proxy (Task 2) that hides Open WebUI's workspace chrome and keeps `#workspace-container` visible.

This is a static file with no runtime behavior, so its "test" is that Task 2's proxy test asserts the proxy serves these exact bytes for an admin request. Write the file now.

- [ ] **Step 1: Create the stylesheet**

Create `chat/embed_admin.css` with this content:

```css
/* Studio admin embed stylesheet.
 *
 * Served as Open WebUI's /static/custom.css *for workspace/admin requests*
 * (the proxy picks this file over chat/embed.css when the request carries the
 * ?__studio_admin=1 marker). Goal: show only the target workspace section
 * (Knowledge, Tools, ...) with no Open WebUI tab nav or chrome, so the iframe
 * reads as a Studio page.
 *
 * This file is served INSTEAD of chat/embed.css for admin requests, so it must
 * carry every hook that still applies in the workspace context. The workspace
 * pages have no chat sidebar, but the "Open Sidebar" toggle button is present
 * and must stay hidden (same hook as embed.css).
 *
 * DRIFT RISK: the tab-nav selector below is structural (main#main-content >
 * div > nav) because Open WebUI gives that <nav> no id or aria-label, only
 * Tailwind classes. An upgrade that changes the DOM depth or adds a sibling
 * nav will silently re-expose the tab bar. Detection is visual: if the
 * Models/Knowledge/Prompts/Skills/Tools tab bar reappears at the top of the
 * embedded frame, re-check the workspace DOM and update the selector. The
 * #workspace-container id is the stable "keep" hook — if it is renamed, the
 * whole section disappears, which is loud and easy to spot.
 */

/* The workspace tab nav (Models / Knowledge / Prompts / Skills / Tools).
 * Structural: it is the first <nav> inside the first child of
 * main#main-content. Hiding it leaves only the section content. */
main#main-content > div > nav {
  display: none !important;
}

/* Redundant fallback: the tab nav is the only element with this class combo,
 * so a DOM-depth change is not enough to re-expose it. */
nav.backdrop-blur-xl.drag-region {
  display: none !important;
}

/* The "Open Sidebar" toggle in the workspace top bar. Same stable id as in
 * chat/embed.css; the workspace has no chat sidebar, but the button is present
 * and would open a blank gap, so it goes. */
#sidebar-toggle-button {
  display: none !important;
}

/* Keep the section content filling the frame. The workspace container is the
 * stable "keep" hook; give it the full height so the masked frame has no dead
 * space where the tab bar was. */
#workspace-container {
  height: 100% !important;
}
```

- [ ] **Step 2: Verify the file exists and is non-empty**

Run: `test -s chat/embed_admin.css && wc -l chat/embed_admin.css`
Expected: a line count > 0 (the file exists and has content).

- [ ] **Step 3: Commit**

```bash
git add chat/embed_admin.css
git commit -m "feat(chat): add embed_admin.css mask for workspace iframe"
```

---

### Task 2: Proxy serves `embed_admin.css` for admin requests

**Files:**
- Modify: `chat/proxy.py` (the `_proxy` method's `custom.css` branch, and the `_serve_embed_css` method)
- Test: `chat/tests/test_proxy.py`

**Interfaces:**
- Consumes: `chat/embed.css` and `chat/embed_admin.css` (both exist after Task 1).
- Produces: the proxy serves `embed_admin.css` bytes when the request path is `/static/custom.css` **and** the request carries a `?__studio_admin=1` marker; otherwise it serves `embed.css` as today. No new public signature — `_serve_embed_css` gains an optional `admin: bool = False` parameter.

The marker travels in the iframe URL's query string. Open WebUI's `app.html` requests `/static/custom.css` (no query), so the *page's own* CSS request has no marker. The marker instead rides on the **initial document request** (`/workspace/knowledge?__studio_admin=1`), and the proxy remembers "this browser asked for admin" via a short-lived per-cookie flag so the subsequent `/static/custom.css` request (same browser, seconds later) is answered with the admin sheet.

- [ ] **Step 1: Write the failing test**

Add to `chat/tests/test_proxy.py` inside `class ProxyTests` (after `test_the_embed_stylesheet_comes_from_studio_not_the_upstream`):

```python
    def test_admin_marker_selects_the_admin_stylesheet(self):
        """A request carrying ?__studio_admin=1 makes the proxy serve
        chat/embed_admin.css (which hides the workspace tab nav) for the
        subsequent /static/custom.css request, instead of chat/embed.css.

        The marker rides on the initial document request; the CSS request
        itself has no query, so the proxy must remember the marker per
        browser (cookie) for a short window.
        """
        # 1. The initial document request carries the marker.
        httpx.get(f"{self.url}/workspace/knowledge?__studio_admin=1",
                  headers={"Cookie": VALID_COOKIE})
        # 2. The page's own CSS request (no query) now gets the admin sheet.
        response = httpx.get(f"{self.url}/static/custom.css",
                             headers={"Cookie": VALID_COOKIE})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response.headers["Content-Type"])
        # embed_admin.css keeps #workspace-container and hides the tab nav.
        self.assertIn("#workspace-container", response.text)
        self.assertIn("main#main-content > div > nav", response.text)

    def test_no_marker_serves_the_chat_stylesheet(self):
        """Without the marker, /static/custom.css is still chat/embed.css."""
        httpx.get(f"{self.url}/workspace/knowledge", headers={"Cookie": VALID_COOKIE})
        response = httpx.get(f"{self.url}/static/custom.css",
                             headers={"Cookie": VALID_COOKIE})
        self.assertEqual(response.status_code, 200)
        self.assertIn("#sidebar", response.text)
        self.assertNotIn("#workspace-container", response.text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest chat/tests/test_proxy.py -q -k "admin_marker or no_marker_serves"`
Expected: FAIL — `test_admin_marker_selects_the_admin_stylesheet` fails because the proxy still serves `embed.css` (no `#workspace-container` in the body).

- [ ] **Step 3: Implement the marker + admin CSS selection**

In `chat/proxy.py`, add a short-lived per-cookie marker cache next to the identity cache (after the `_identity_lock` definition, around line 60):

```python
# The ?__studio_admin=1 marker rides on the initial document request; the
# page's own /static/custom.css request has no query, so remember "this
# browser asked for the admin sheet" for a short window, keyed on the cookie
# (same identity the proxy already uses). Short on purpose, like IDENTITY_TTL.
ADMIN_MARKER_TTL = float(os.environ.get("SIMPLEAUDIT_CHAT_ADMIN_MARKER_TTL", "30"))
_admin_marker: dict[str, float] = {}
_admin_marker_lock = threading.Lock()


def _mark_admin(cookie: str) -> None:
    if ADMIN_MARKER_TTL <= 0:
        return
    with _admin_marker_lock:
        if len(_admin_marker) > 1024:
            _admin_marker.clear()
        _admin_marker[cookie] = time.monotonic() + ADMIN_MARKER_TTL


def _is_admin(cookie: str) -> bool:
    if ADMIN_MARKER_TTL <= 0:
        return False
    with _admin_marker_lock:
        expiry = _admin_marker.get(cookie)
        if expiry is None:
            return False
        if expiry < time.monotonic():
            _admin_marker.pop(cookie, None)
            return False
        return True
```

In the `_proxy` method, in the `custom.css` branch (currently around lines 128-131), select the sheet by the marker:

```python
        if self.command == "GET" and urlsplit(self.path).path == "/static/custom.css":
            self._serve_embed_css(admin=_is_admin(self.headers.get("Cookie", "")))
            return
```

And, earlier in `_proxy` (right after `cookie = self.headers.get("Cookie", "")`, around line 141), record the marker when the document request carries it:

```python
        cookie = self.headers.get("Cookie", "")
        if "__studio_admin=1" in urlsplit(self.path).query:
            _mark_admin(cookie)
```

Update `_serve_embed_css` to pick the file (currently around line 180):

```python
    def _serve_embed_css(self, admin: bool = False) -> None:
        """Serve Studio's embed stylesheet as Open WebUI's /static/custom.css.

        ``admin`` selects chat/embed_admin.css (workspace mask) over
        chat/embed.css (chat mask). The bytes come from this repo, not the
        upstream, so the embed styling survives Open WebUI upgrades and lives
        in one place shared with the Docker mode (which mounts the same file
        for Caddy).
        """
        filename = "embed_admin.css" if admin else "embed.css"
        css = (Path(__file__).parent / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(css)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(css)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest chat/tests/test_proxy.py -q`
Expected: PASS — all proxy tests pass, including the two new ones and the existing `test_the_embed_stylesheet_comes_from_studio_not_the_upstream` (which still gets `embed.css` because it sends no marker).

- [ ] **Step 5: Commit**

```bash
git add chat/proxy.py chat/tests/test_proxy.py
git commit -m "feat(chat): serve embed_admin.css when the admin marker is set"
```

---

### Task 3: `AgentResourcesView` + template + URL

**Files:**
- Modify: `infra/ui.py` (add `AgentResourcesView` near the other agent views, around line 2687)
- Create: `templates/infra/agents/resources.html`
- Modify: `config/urls.py` (add the `/agents/resources/` URL near the other agent URLs, lines 203-208)
- Test: `infra/tests/test_agent_resources.py`

**Interfaces:**
- Consumes: `ProjectMixin` (already imported in `infra/ui.py`), the chat proxy's `public_url()` from `chat/config.py`, and the verified Open WebUI workspace routes.
- Produces: `AgentResourcesView` (a `ProjectMixin, TemplateView`) at `/agents/resources/` that renders `infra/agents/resources.html` with context: `section` (one of `knowledge`/`tools`), `iframe_src` (the proxy URL to the workspace page with the `__studio_admin=1` marker), `sections` (the list of available sections), and `open_webui_url` (the full-window fallback).

The view builds the iframe URL from the chat proxy's public base URL + the workspace route + the marker. If chat is disabled, it renders a "chat disabled" state (no iframe).

- [ ] **Step 1: Write the failing test**

Create `infra/tests/test_agent_resources.py`:

```python
"""Tests for the /agents/resources/ restricted-iframe resource manager."""
from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from infra.tests.factories import ProjectFactory, UserFactory


def _member(project, *, is_admin=True):
    user = UserFactory()
    ProjectMembership.objects.create(
        project=project, user=user,
        role=ProjectMembership.Role.ADMIN if is_admin else ProjectMembership.Role.MEMBER,
    )
    return user


class AgentResourcesViewTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()

    def _login(self, user):
        self.client.force_login(user)

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_member_gets_the_page_with_the_knowledge_iframe(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "infra/agents/resources.html")
        # Default section is knowledge; the iframe points at the workspace
        # knowledge page through the proxy with the admin marker.
        self.assertIn("/workspace/knowledge", resp.content.decode())
        self.assertIn("__studio_admin=1", resp.content.decode())

    def test_section_param_selects_the_tools_iframe(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/resources/?section=tools")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/workspace/tools", resp.content.decode())
        self.assertIn("__studio_admin=1", resp.content.decode())

    def test_unknown_section_falls_back_to_knowledge(self):
        user = _member(self.project)
        self._login(user)
        resp = self.client.get("/agents/resources/?section=bogus")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("/workspace/knowledge", resp.content.decode())

    def test_non_member_cannot_view(self):
        other = UserFactory()
        self._login(other)
        resp = self.client.get("/agents/resources/")
        self.assertIn(resp.status_code, (302, 403, 404))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest infra/tests/test_agent_resources.py -q`
Expected: FAIL — `test_member_gets_the_page_with_the_knowledge_iframe` fails with 404 (no URL/view yet).

- [ ] **Step 3: Add the view**

In `infra/ui.py`, add `AgentResourcesView` immediately after `AgentsView` (find `class AgentsView` around line 2687 and insert after its closing brace). Ensure `TemplateView` is imported (add to the existing `from django.views.generic import ...` line if not present):

```python
class AgentResourcesView(ProjectMixin, TemplateView):
    """Restricted-iframe resource manager.

    Embeds Open WebUI's workspace admin (Knowledge / Tools) in an iframe,
    masked to the target section by chat/embed_admin.css. The iframe points at
    the chat proxy with the ?__studio_admin=1 marker so the proxy serves the
    admin sheet. Studio never writes here; the pull-sync keeps local models
    fresh.
    """
    template_name = "infra/agents/resources.html"

    # section -> Open WebUI workspace route (verified against the live instance)
    SECTIONS = {
        "knowledge": "/workspace/knowledge",
        "tools": "/workspace/tools",
    }
    DEFAULT_SECTION = "knowledge"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        section = self.request.GET.get("section", self.DEFAULT_SECTION)
        if section not in self.SECTIONS:
            section = self.DEFAULT_SECTION
        ctx["section"] = section
        ctx["sections"] = [
            {"key": "knowledge", "label": "Knowledge"},
            {"key": "tools", "label": "Tools"},
        ]
        from chat import config as chat_config
        if chat_config.ENABLED:
            base = chat_config.public_url()
            ctx["iframe_src"] = f"{base}{self.SECTIONS[section]}?__studio_admin=1"
            ctx["open_webui_url"] = f"{base}{self.SECTIONS[section]}"
            ctx["chat_enabled"] = True
        else:
            ctx["iframe_src"] = None
            ctx["open_webui_url"] = None
            ctx["chat_enabled"] = False
        return ctx
```

- [ ] **Step 4: Create the template**

Create `templates/infra/agents/resources.html`. First check the base template the other agent pages extend — look at `templates/infra/agents/` for an existing template (e.g. `list.html`) and extend the same base. If none exists, extend the project's main base (`{% extends "base.html" %}` or whatever `AgentsView` uses). Match the existing agent-page structure. Content:

```html
{% extends "base.html" %}
{% block title %}Agent Resources{% endblock %}
{% block content %}
<div class="resources-page">
  <div class="resources-topbar">
    <a href="{% url 'agents-list' %}" class="back-link">&larr; Agents</a>
    <nav class="resources-tabs">
      {% for s in sections %}
      <a href="?section={{ s.key }}"
         class="tab {% if s.key == section %}active{% endif %}">{{ s.label }}</a>
      {% endfor %}
    </nav>
    <div class="resources-actions">
      {% if chat_enabled and open_webui_url %}
      <a href="{{ open_webui_url }}" target="_blank" rel="noopener" class="open-owui">
        Open in Open WebUI
      </a>
      {% endif %}
    </div>
  </div>

  {% if chat_enabled %}
  <iframe class="resources-frame" src="{{ iframe_src }}" title="Open WebUI {{ section }}"></iframe>
  {% else %}
  <div class="resources-disabled">
    Chat (Open WebUI) is not enabled on this server, so resources cannot be
    managed here. Enable it with <code>SIMPLEAUDIT_CHAT=embedded</code>.
  </div>
  {% endif %}
</div>
{% endblock %}
```

> **Note for the implementer:** if the existing agent templates extend a different base or use a different block name (e.g. `{% block main %}`), match them. The `{% url 'agents-list' %}` name must match the actual URL name for the agents list in `config/urls.py` — check the existing agent URL names and use the correct one. Add minimal CSS for `.resources-page`, `.resources-topbar`, `.resources-tabs`, `.resources-frame` (iframe should fill the remaining viewport height) in the page or a small `<style>` block; keep it consistent with the project's existing styling approach.

- [ ] **Step 5: Add the URL**

In `config/urls.py`, near the other agent URLs (lines 203-208), add:

```python
    path("agents/resources/", infra_ui.AgentResourcesView.as_view(), name="agents-resources"),
```

Match the existing import style for the view (the file already imports the agent views from `infra.ui` — add `AgentResourcesView` to that import).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest infra/tests/test_agent_resources.py -q`
Expected: PASS — all five tests pass.

- [ ] **Step 7: Commit**

```bash
git add infra/ui.py templates/infra/agents/resources.html config/urls.py infra/tests/test_agent_resources.py
git commit -m "feat(infra): add /agents/resources/ restricted-iframe resource manager"
```

---

### Task 4: Navigation — "Resources" submenu under Agents

**Files:**
- Modify: `infra/context_processors.py` (the `_NAV` tuple, lines 89-101)
- Modify: `templates/partials/sidebar.html` (lines 49-53, the nav rendering)
- Test: `infra/tests/test_agent_resources.py` (extend with a nav test)

**Interfaces:**
- Consumes: the `nav()` context processor and the `agents-resources` URL name (Task 3).
- Produces: a "Resources" entry rendered as an indented sub-item under "Agents" in the sidebar, active when the path starts with `/agents/resources`.

The sidebar currently renders a flat list from `nav_items`. Add a `children` concept: the Agents entry gets a `children` list containing the Resources sub-item. The template renders children indented.

- [ ] **Step 1: Write the failing test**

Append to `infra/tests/test_agent_resources.py`:

```python
class AgentResourcesNavTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = _member(self.project)
        self.client.force_login(self.user)

    def test_resources_appears_in_nav_context(self):
        resp = self.client.get("/agents/resources/")
        self.assertEqual(resp.status_code, 200)
        # The sidebar renders a Resources link to the resource manager.
        self.assertIn(b"agents/resources", resp.content)
        self.assertIn(b"Resources", resp.content)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest infra/tests/test_agent_resources.py -q -k Nav`
Expected: FAIL — `b"agents/resources"` not in the page (nav not wired yet).

- [ ] **Step 3: Add the nav entry**

In `infra/context_processors.py`, look at the `_NAV` tuple (lines 89-101). The Agents entry is a tuple like `("Agents", "/agents/", "agents")` (or similar — read the exact shape first). Add a Resources sub-item. The cleanest approach that matches the existing flat structure: add a new entry for Resources that the template renders indented under Agents. Add to `_NAV` (after the Agents entry):

```python
    ("Resources", "/agents/resources/", "agents-resources", "agents"),
```

where the 4th element is the parent key (used by the template to indent). If the existing `_NAV` entries are 3-tuples, extend the `nav()` function to carry the optional parent. Read the actual `_NAV` shape and `nav()` implementation first and adapt — the goal is: the rendered `nav_items` includes a Resources item flagged as a child of Agents.

- [ ] **Step 4: Render the sub-item in the sidebar**

In `templates/partials/sidebar.html` (lines 49-53), the nav loop renders each item. Add handling so an item with a parent is rendered indented (e.g. wrapped in a `<li class="nav-subitem">` with extra left padding). Match the existing markup/classes. The Resources item should link to `{% url 'agents-resources' %}` and be marked active when the current path starts with `/agents/resources` (the existing `score()` longest-prefix logic in `nav()` already handles active state by path prefix, so `/agents/resources` will be more specific than `/agents/` and win).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest infra/tests/test_agent_resources.py -q`
Expected: PASS — all tests pass including the nav test.

- [ ] **Step 6: Commit**

```bash
git add infra/context_processors.py templates/partials/sidebar.html infra/tests/test_agent_resources.py
git commit -m "feat(infra): add Resources submenu under Agents in the sidebar"
```

---

### Task 5: End-to-end verification against the live instance

**Files:**
- None (verification only). If the live check reveals a selector/route drift, fix the affected file from Tasks 1-4 and re-run its tests.

**Interfaces:**
- Consumes: the running dev server (web on 8000, proxy on 8801, Open WebUI on 8080) started with `SIMPLEAUDIT_CHAT=embedded uv run manage.py dev_server --embedded`.
- Produces: confirmation that the iframe renders the masked workspace page and that a resource created in the iframe appears after a sync.

- [ ] **Step 1: Confirm the server is running**

Run: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ && echo " web" && curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8801/static/custom.css && echo " proxy-css"`
Expected: `200 web` and `200 proxy-css`. If not running, start it: `SIMPLEAUDIT_CHAT=embedded uv run manage.py dev_server --embedded` (background) and wait for "Chat is ready".

- [ ] **Step 2: Verify the proxy serves the admin sheet for a marked request**

Run:
```bash
curl -s "http://127.0.0.1:8801/workspace/knowledge?__studio_admin=1" -H "Cookie: <studio-session>" -o /dev/null
curl -s "http://127.0.0.1:8801/static/custom.css" -H "Cookie: <studio-session>" | grep -c "workspace-container"
```
(Use a real Studio session cookie; obtain one by logging in via the browser or `manage.py shell`.)
Expected: the second command prints `1` (the admin sheet, with `#workspace-container`, is served after the marked request).

- [ ] **Step 3: Verify the page renders in a browser**

Open `http://127.0.0.1:8000/agents/resources/` in the integrated browser (signed in as the `studio` admin). Confirm:
- The iframe loads the Open WebUI Knowledge workspace page.
- The Models/Knowledge/Prompts/Skills/Tools tab bar is **hidden** (only the knowledge list + search + Create show).
- The "Open Sidebar" button is not visible.
- The "Tools" tab switches the iframe to `/workspace/tools`.
- "Open in Open WebUI" opens the full page in a new tab.

- [ ] **Step 4: Verify the pull-sync picks up a created resource**

In the iframe, create a Knowledge Base (upload a small text file). Then trigger a sync: `POST /agents/sync/` (or click the sync control in the UI). Confirm a new `KnowledgeBase` row appears:
```bash
uv run manage.py shell -c "from model_registry.models import KnowledgeBase; print(list(KnowledgeBase.objects.values_list('name','external_id')))"
```
Expected: the new KB's name and external_id are listed.

- [ ] **Step 5: Commit any drift fixes**

If Step 2-4 revealed a route or selector drift, fix the relevant file and run its tests, then:
```bash
git add -A
git commit -m "fix(infra): correct workspace route/selector for resource iframe"
```
If nothing needed fixing, skip this step.

---

## Self-Review

**Spec coverage:**
- §5 (restricted iframe, delegate to Open WebUI) → Tasks 1-3.
- §6 (architecture: iframe → proxy → workspace page, no push) → Tasks 2-3.
- §7.1 (`AgentResourcesView`) → Task 3.
- §7.2 (`embed_admin.css` mask) → Task 1.
- §7.4 (workspace routes per section) → Task 3 (`SECTIONS`), verified in Global Constraints.
- §7.5 ("Open in Open WebUI" fallback) → Task 3 (template).
- §7.6 (Resources submenu under Agents) → Task 4.
- §8 (error handling: chat disabled, mask break) → Task 3 (chat-disabled state), Task 5 (drift check).
- §9 (testing) → Tasks 2, 3, 4 (unit) + Task 5 (e2e).
- §11 (phasing: knowledge first, then tools, then MCP) → Tasks 3-4 cover knowledge + tools; MCP is documented as not-exposed-in-this-version (Global Constraints) and surfaced as a note in the Tools section.

**Placeholder scan:** No TBD/TODO. The one implementer note in Task 3 Step 4 (match the existing base template / URL name) is an explicit instruction to read the existing files, not a placeholder — the agent-page templates and URL names exist and must be matched.

**Type consistency:** `AgentResourcesView.SECTIONS` keys (`knowledge`/`tools`) match the template's `sections` list and the test assertions. `iframe_src`/`open_webui_url`/`chat_enabled` context keys are consistent between the view (Task 3 Step 3) and template (Step 4). The proxy's `_serve_embed_css(admin=...)` signature (Task 2) matches its call site.

**MCP scope note:** The spec lists MCP as a section, but the verified live instance has no MCP workspace page or API. The plan surfaces MCP as a note in the Tools section rather than a broken iframe link, and documents this in Global Constraints. If a future Open WebUI version adds `/workspace/mcp`, add it to `AgentResourcesView.SECTIONS` and the `sections` list — no other changes needed.
