# Open WebUI subpath: distribution & maintenance design

Status: **decision record** — 2026-10-04. Complements `README.md` (how to build
today) and `UPGRADE.md` (how to move to a new upstream version).

## The decision

**Distribute the subpath build as a Docker image, produced from a fork of
`open-webui/open-webui`.** For SimpleAudit Studio we currently do not even need
to publish the fork: `build.sh` in this directory rebuilds the identical image
from the upstream repo + this directory's patch, so the *consumer* side is
complete as-is. The fork is the vehicle for **publishing** (CI → `ghcr.io`) and
for making the upgrade loop one push instead of a local command.

Channel ranking:

1. **Docker image (`ghcr.io/sushantgautam/open-webui:vX.Y.Z-chat`)** — primary.
   Matches how Studio consumes OWUI (`docker-compose.yml` image) and how
   upstream itself ships (its `docker.yaml` workflow).
2. **`pip install`** — secondary, and *only* if it is fixed (see below).
   Not a viable channel today.
3. Repo of prebuilt script bundles — rejected (see below).

## Two verified facts that shape everything

### 1. The subpath is baked at build time, not runtime

`svelte.config.js` and `vite.config.ts` read `process.env.WEBUI_SUBPATH` and
freeze it into the emitted JS (`base: '/chat'`, `relative: false`);
`src/lib/constants.ts` bakes `WEBUI_BASE_URL = base` into every API/asset URL.

Consequence: **one built artifact = one subpath.** A `/chat` bundle cannot be
served at another prefix without rebuilding. This is fine (Studio always uses
`/chat`), but it rules out "ship one universal bundle".

### 2. Upstream's PyPI wheel ships NO frontend

`pyproject.toml` line 208:

```toml
force-include = { "CHANGELOG.md" = "open_webui/CHANGELOG.md", build = "open_webui/frontend" }
```

but `.github/workflows/release-pypi.yml` never runs `npm run build` before
`python -m build .` — there is no `build/` directory to force-include, so the
upstream `open-webui` wheel on PyPI is **API-only**. `backend/open_webui/main.py:3027`
then logs `Frontend build directory not found ... Serving API only.`

So "install from my repo with `pip install`" is not currently a real channel
for anyone, upstream included. It only becomes real if a release workflow does:

```
npm ci && WEBUI_SUBPATH=/chat npm run build   # creates ./build with /chat baked in
python -m build .                              # wheel now bakes ./build → open_webui/frontend
```

If we ever ship this: the PyPI project must be renamed (e.g. `open-webui-subpath`)
because `open-webui` is taken, and every version needs a fresh npm build.

## Why not "my repo of prebuilt scripts"

Option A (commit the built `*.js` bundles + patch + notes to a repo, rebuild by
asking an agent) is strictly worse than a fork:

- The minified bundle is the *only* source of truth — no clean base to rebuild
  from when a new upstream version lands. The agent must produce a full frontend
  build anyway, which means checking out upstream source, applying the patch,
  and running `npm run build` — i.e. it is already rebuilding a fork, just
  without the git scaffolding that makes that reproducible.
- Bundles in git are 100s of KB of unmaintainable churn; a patch is reviewable.
- `git apply --check` gives a hard, machine-verifiable "does this patch still
  fit upstream?" gate. A repo of blobs has no such signal.

## The fork layout (PUBLISHED 2026-10-04)

```
github.com/sushantgautam/open-webui     ← fork of open-webui/open-webui
│   main:  upstream v0.11.4 (8bd8b4fac) + the subpath commits
│   dev:   pristine upstream v0.11.4 (kept clean as the re-align target)
│
├── subpath support (merge commit 42ac2ae70)  ← the 121-file feature PORTED to
│   v0.11.4 (123 files, +1017/−305); merge-both resolution, E2E-verified
├── docker: node heap 6GB (c1674863c)         ← NODE_OPTIONS=--max-old-space-size=6144
├── docs: FORK.md (f3755b1b1)                 ← fork purpose + build/publish/upgrade
├── ci: publish to sushantgautam (217f36f36)  ← IMAGE_REPOSITORY → sushantgautam/open-webui
│
├── .github/workflows/docker.yaml       ← upstream's, with:
│                                          IMAGE_REPOSITORY → sushantgautam/open-webui
│                                          webui_subpath dispatch input → `-subpath` tag
│                                          suffix (root tags stay clean)
│
├── .github/workflows/release-pypi.yml  ← OPTIONAL. If kept: add
│                                          `npm ci && WEBUI_SUBPATH=/chat npm run build`
│                                          before `python -m build`; rename project
│                                          to open-webui-subpath. Otherwise delete.
│
├── .github/workflows/catch-up.yml      ← manual trigger: see UPGRADE.md for the
│                                          exact steps; the workflow automates
│                                          fetch upstream tag → rebase →
│                                          apply --check → build → smoke → tag
└── README.md                           ← "what is this fork, how it differs,
                                          how to rebuild at a new version"
```

Consumer side (already done, in this directory):

- `build.sh` — local build from upstream @ pinned SHA + patch (no fork needed)
- `Caddyfile.subpath` — one-origin proxy, no prefix strip
- `docker-compose.yml` — point `OPEN_WEBUI_IMAGE` at `ghcr.io/sushantgautam/...`
  once the fork exists; until then at the locally built
  `simpleaudit/open-webui-subpath:chat`

## Upgrade loop (the "ask the agent" workflow)

The unit of work when upstream releases a new version:

```
1. git fetch upstream <new-tag>            # in the fork
2. git rebase upstream/<new-tag>           # replay the 2 patch commits
   (or: fresh clone @ tag + git apply subpath.patch)
3. git apply --check subpath.patch         # HARD GATE — CI fails immediately
   if the patch no longer fits
4. WEBUI_SUBPATH=/chat npm run build       # frontend compiles?
5. docker build + smoke: /chat/health 200,
   SPA has 0 unprefixed root refs, WS /chat/ws/socket.io → 101,
   one LLM round-trip
6. tag v<upstream-ver>-chat → CI pushes ghcr.io
7. Studio: bump OPEN_WEBUI_IMAGE, re-run studio smoke (SSO + chat)
```

Drift data point (now resolved): against v0.11.4 (`8bd8b4f`) the v0.9.6
patch applied cleanly to only 33/121 files, 88 with conflicts (8 of the 9
core files). Ported on 2026-10-04 as a **git merge** (not `apply --3way`,
which aborts atomically on files upstream deleted) — 79 files resolved
merge-both. Porting a minor version is real work (~1 agent-hour) — that's
*why* the loop must keep the
`apply --check` + smoke gate, and why we pin and upgrade deliberately rather
than tracking `main`.

## Open questions (parked, not blockers)

- ~~Publish the fork?~~ → **Done** (2026-10-04): `sushantgautam/open-webui`
  `main` = v0.11.4 + subpath, `dev` = pristine v0.11.4; CI publishes root
  images on push, subpath on `webui_subpath` dispatch.
- If pip channel is ever wanted: `open-webui-subpath` project name; who
  maintains PyPI token/Trusted Publisher.
- Tag suffix convention settled on `-subpath` (matches the fork's CI input).
