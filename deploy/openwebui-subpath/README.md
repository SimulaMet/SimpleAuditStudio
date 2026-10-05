# Open WebUI — subpath build for SimpleAudit Studio

This directory is the self-contained, reproducible build that lets **Open WebUI
run under a URL subpath** (e.g. `/chat`) so it shares one origin and one port
with the Studio Django app, instead of living on a separate cross-origin host
embedded in an iframe.

It is the working path that spencercjh implemented in their fork
([spencercjh/open-webui](https://github.com/spencercjh/open-webui)) — here as
**one auditable patch + a build script**, not a fork to maintain by hand.

## Why a patch is needed at all

Stock Open WebUI bakes every reference to the **site root**:

```
/static/favicon.png   /_app/immutable/entry/start.js
/api/v1/config        /ws/socket.io
```

Put it behind a reverse proxy at `/chat/` and the browser still asks for
`/static/...`, `/api/...`, `/ws/socket.io` — none of which match the `/chat`
prefix, so they fall through to the host app (Django) and the page is a white
screen. Proxy rewriting alone cannot fix this because new hardcoded routes keep
appearing (the classic nginx `sub_filter` whack-a-mole).

The fix is to **build Open WebUI so the frontend and backend both know their
base path is `/chat`**, so it *generates* URLs under `/chat`. That is exactly
what `subpath.patch` does.

## What's in this directory

| File | Purpose |
|---|---|
| `subpath.patch` | The subpath change (121 files, +631/−296). Verified to apply **cleanly** to the pinned base and to reproduce the fork's tree byte-for-byte. |
| `0002-node-build-heap.patch` | Tiny build fix: `ENV NODE_OPTIONS=--max-old-space-size=6144`. The SvelteKit/Vite production build OOMs at Node's ~2GB default heap on constrained builders. |
| `build.sh` | Clones the pinned upstream, applies both patches, builds with the upstream project's own (now-patched) Dockerfile. |
| `Caddyfile.subpath` | The single-origin routing (forward-auth + `handle /chat/*` no-strip + fallthrough to Studio). Reference for cutover. |
| `DESIGN.md` | Distribution & maintenance decision record (fork vs. artifacts repo, why Docker/ghcr.io is the channel, why `pip install` is a trap as-is). |
| `UPGRADE.md` | Step-by-step procedure + copy-paste agent prompt for rebasing the patch onto a new upstream version. |

## Build

```bash
cd deploy/openwebui-subpath
./build.sh                 # -> simpleaudit/open-webui-subpath:chat  (WEBUI_SUBPATH=/chat)
SUBPATH=/openwebui ./build.sh   # different prefix
```

The script **stops with a clear error if the patch does not apply** to the
pinned SHA. The upstream Dockerfile gets `WEBUI_SUBPATH` as a build *and*
runtime arg purely from the patch — there is no custom Dockerfile here.

## Route (cutover)

`Caddyfile.subpath` is the new shape. The load-bearing line:

```caddy
handle /chat/* {                      # `handle`, NOT `handle_path`
	reverse_proxy open-webui:8080
}
handle {                               # everything else -> Studio
	reverse_proxy web:8000
}
```

`handle_path` (which strips the prefix) **breaks the WebSocket**: the subpath
fork serves its socket at the full `/chat/ws/socket.io` (engineio matches the
raw path, not the `root_path`-relative one), so a stripped request 404s.
`handle` forwards `/chat/...` as-is, which is what the build expects. This was
verified live: raw WS handshake through Caddy returned `101 Switching
Protocols`.

## Verified (empirically)

Against a real Caddy + this image + a mock OpenAI endpoint, in a real browser:

- `GET /chat/` → 200 SPA with **65 `/chat/`-prefixed refs, 0 root-relative**
- Raw WebSocket upgrade `/chat/ws/socket.io` → **101**
- Fallthrough: `/dashboard/`, `/audits/`, `/api/*` → Studio (Studio's log shows
  only the explicit probes — zero OWUI leakage)
- Trusted-header SSO sign-in (the same `/chat/authz` endpoint the existing
  forward-auth proxy uses)
- **Real LLM conversation**: registered an endpoint, sent "Say hello", got the
  streamed reply back at `/chat/c/<id>`; backend log `POST /chat/api/chat/completions → 200`
- Patch reproducibility: fresh `git clone` of upstream → `git apply` → **all 9
  core files byte-identical to the fork**

## The patch, at a glance

~9 files carry the logic; ~112 Svelte files are mechanical `base` prefixing.

- `backend/.../env.py` — reads + validates `WEBUI_SUBPATH`
- `backend/.../main.py` — `FastAPI(root_path=WEBUI_SUBPATH)`; manifest/swagger
  URLs prefixed; `/api/v1/config` returns `subpath`
- `backend/.../__init__.py` — `uvicorn.run(root_path=WEBUI_SUBPATH)` (serve + dev)
- `backend/.../socket/main.py` — `socketio_path=f'{WEBUI_SUBPATH}/ws/socket.io'`
- `backend/.../utils/asgi_middleware.py`, `routers/models.py` — keep `root_path` in redirects
- `svelte.config.js` — `paths = { base: WEBUI_SUBPATH, relative: false }`
- `vite.config.ts` — `base: WEBUI_SUBPATH`
- `src/lib/constants.ts` — `WEBUI_BASE_URL = base` (the single choke point for all API calls)
- `src/app.html` — hardcoded `/static/...` → `%sveltekit.assets%/...`
- `Dockerfile` — `ARG`/`ENV WEBUI_SUBPATH` (build + runtime)
- `src/routes/+layout.svelte` + ~111 more — `goto`/`href`/`location.href` → `${base}/...`;
  socket.io subpath goes on `path`, never the namespace arg

## Upgrading (when you bump `SHA`)

```bash
# 1. Dry-run the patch against the new upstream commit
git clone --filter=blob:none --no-checkout https://github.com/open-webui/open-webui.git o
git -C o checkout <new-sha>
git -C o apply --check subpath.patch      # non-zero = needs a rebase
```

**Full procedure: [`UPGRADE.md`](./UPGRADE.md)** — including a copy-paste
agent prompt with the subpath invariants to preserve and the evidence bar to
hit. v0.11.4 drift was 33/121 files clean, 88 with conflicts (8 of the 9 core
files) and was **ported on 2026-10-04** (see UPGRADE.md outcome log). Treat
each upstream bump as a porting task, not a re-apply. **Do not** promote a
SHA until `apply --check` passes and the E2E (WS 101 + one LLM round-trip)
re-passes. Distribution context (fork, ghcr.io, pip caveats): [`DESIGN.md`](./DESIGN.md).

## Status

**Built, E2E-verified, published, and wired in (2026-10-04).**
`subpath.patch` is the v0.11.4 port (123 files) pinned to `8bd8b4f`; the fork
`github.com/sushantgautam/open-webui` ships `main` = v0.11.4 + subpath with CI
publishing to ghcr.io (root on push, `-subpath` on `webui_subpath` dispatch).

The cutover is done: `docker-compose.yml` pulls
`ghcr.io/sushantgautam/open-webui:v0.11.4-subpath` (set `WEBUI_SUBPATH=/chat`),
`chat-proxy` (Caddy, no profile) is the **only published port** and runs
`Caddyfile.subpath`, Studio's wrapper page moved to `/ai/`, and
`Caddyfile.chat` + the separate cross-origin port are retired. Embedded
(no-Docker) mode runs the same Caddy forward-auth — the bundled `caddyserver`
wheel (all platforms, incl. Windows) — on its own port.
Both modes' `/static/custom.css` requests go to Studio's `/chat/css-mask`,
which serves the admin skin per session (no Caddy-side state).
