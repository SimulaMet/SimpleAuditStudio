# Extensions

Extensions let an operator put small companion tools next to Studio without
forking its templates: sidebar menu items, stylesheets and scripts on Studio
pages, embed pages that wrap another app in the Studio shell, and a
forward-auth endpoint so a companion app can reuse Studio's login.

Everything is **off by default**. With `SIMPLEAUDIT_EXTENSIONS` unset, Studio
renders exactly what it renders without the feature, and `/x/*` and
`/ext/authz` return 404.

## Trust model

The manifest is **operator-controlled code, not user input**. Read this before
you enable it:

- Extension scripts run with **full same-origin privileges**: they can read
  every page the user can see and call every API as that user (they get the
  CSRF token in `window.Studio`). Only list scripts you would merge into
  Studio itself.
- The manifest comes from the deployment's configuration (an environment
  variable or a file), never from the database or the UI. Changes should go
  through the same review as any other deployment change. A database-backed
  editor for scripts is deliberately not provided: storing admin-edited
  JavaScript is a stored-XSS risk by design.
- Scripts are not sandboxed. For third-party URLs, pin them with `integrity`.
- A companion app protected by `/ext/authz` must be reachable **only through
  the proxy**, so clients cannot forge the `X-Studio-*` headers. This is the
  same rule as for the embedded Open WebUI (see [chat.md](chat.md)).

## Configuration

`SIMPLEAUDIT_EXTENSIONS` holds either inline JSON or the path of a JSON file.
A value starting with `[` or `{` is read as JSON; anything else is a file path.
The file is re-read when it changes, without a restart.

The JSON is a list of entries, or `{"extensions": [...]}`. An invalid manifest
is logged as an error (`simpleaudit.extensions`) and ignored; it never takes
the UI down. A complete example is in
[examples/extensions.json](examples/extensions.json), with its assets in
`static/extensions/example/`.

| Field | Required | Meaning |
|---|---|---|
| `slug` | yes | Lowercase letters, digits and dashes; unique. Used in `/x/<slug>/`. |
| `label` | no | Menu text. Defaults to the slug. |
| `icon` | no | Menu icon (an emoji or symbol). Defaults to `◇`. |
| `section` | no | Heading the item is grouped under in the sidebar. Entries without one are grouped together, without a heading. |
| `order` | no | Integer sort key within a section (default 100), then label. |
| `roles` | no | Who sees the entry; default `["admin"]`. See [Roles](#roles). |
| `href` | no | Menu link: an `http(s)://` URL or a path starting with `/`. |
| `embed` | no | A **same-origin** path (starting with `/`) shown in an iframe at `/x/<slug>/`. The menu item points at that page. Give `href` or `embed`, not both. |
| `css`, `js` | no | A URL or a list of URLs (`http(s)://` or `/` paths). |
| `pages` | no | Glob patterns on the request path (`/runs/*`) limiting where `css`/`js` load. Default: every page. |
| `integrity` | no | Map of asset URL to a Subresource Integrity hash (`sha256-`, `sha384-` or `sha512-`). |

An entry with neither `href` nor `embed` adds no menu item; use that for
asset-only extensions.

### Roles

Roles form a ladder: `user` < `viewer` < `auditor` < `admin` < `superuser`.
A user's level is the highest role they hold in **any** workspace; any signed-in
user is at least `user`, and a Django superuser is `superuser`. An entry is
shown to users at or above the lowest role it lists. Entries are instance-wide:
there are no per-workspace manifests.

Filtering happens on the server. An entry a user may not see is absent from
the HTML, its assets are not loaded for that user, and its embed page returns
404, the same as an unknown slug.

## What Studio renders

**Menu items** appear in the sidebar below the built-in pages, grouped by
`section`.

**Slots.** Stylesheets are emitted at the end of `<head>`. At the end of
`<body>` Studio emits, in order:

1. `window.Studio`: the page context (below), from a
   `<script type="application/json" id="studio-page-context">` block.
2. The extension scripts, each with `defer` (and `integrity` plus
   `crossorigin="anonymous"` when a hash is given).
3. A `studio:ready` event on `document`, dispatched at `DOMContentLoaded`,
   after every deferred extension script has run.

Every element Studio adds carries a `data-studio-extension` attribute.

### `window.Studio`

| Field | Meaning |
|---|---|
| `version` | Studio version. |
| `user` | `{id, username, name, email}`. |
| `role` | The user's level on the role ladder (`user` … `superuser`). |
| `workspace` | `{id, name}` of the active workspace, or `null`. |
| `csrfToken` | Send as `X-CSRFToken` on unsafe requests to Studio's API. |
| `path` | The request path the page was rendered for. |
| `extensions` | Slugs of the extensions whose assets load on this page. |
| `onPage(pattern, fn)` | Runs `fn(Studio)` once ready if `location.pathname` matches the glob (or any glob in a list). |

```js
Studio.onPage("/runs/*", (studio) => {
  console.log("run page for", studio.user.username, "in", studio.workspace?.name);
});
document.addEventListener("studio:ready", (e) => { /* e.detail === window.Studio */ });
```

Studio sends no Content-Security-Policy, so no nonce is needed. If you add a
CSP at your proxy, allow the script origins you list and Studio's inline
scripts.

## Embed pages

`/x/<slug>/` renders the normal Studio shell (sidebar, top bar, footer) around
an iframe of the entry's `embed` path. Signed-out users are sent to the login
page. Studio sends `X-Frame-Options: SAMEORIGIN`, so serve the companion app
on the same origin, for example under `/feedback/` behind the same proxy.

## Forward auth: `GET /ext/authz`

A companion app behind any forward-auth-capable proxy can use Studio's login.
The contract matches `/chat/authz`:

| Session | Response |
|---|---|
| Signed in, at or above `?role=` (default `admin`) | `200` with `X-Studio-Email`, `X-Studio-Name`, `X-Studio-Role` (`admin` or `user`) and `X-Studio-Username` |
| Signed out | `401`, with `X-Studio-Login` set to the login URL (carrying `next` from `X-Forwarded-Uri` when the proxy sends it) |
| Signed in, below the role | `403` |
| Unknown `role` value | `400` |

Caddy example protecting a companion app at `/feedback/` for admins:

```caddyfile
handle /feedback/* {
	forward_auth studio:8000 {
		uri /ext/authz?role=admin
		copy_headers X-Studio-Email X-Studio-Name X-Studio-Role X-Studio-Username
		@signedout status 401
		handle_response @signedout {
			redir * /login/?next={uri} 302
		}
	}
	reverse_proxy feedback-app:3000
}
```

Strip any client-supplied `X-Studio-*` headers before the request reaches the
companion app (Caddy's `copy_headers` overwrites them), and do not expose the
companion app on any other route.
