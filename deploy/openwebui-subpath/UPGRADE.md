# Upgrading the subpath Open WebUI to a new upstream version

Run this when you want to move the subpath build to a newer upstream version.
**Current pinned base: `8bd8b4f` (v0.11.4)** — the subpath feature is merged
on top as commit `42ac2ae70`. The design and rationale live in `DESIGN.md`.

## Drift experience (measured 2026-10-04)

Porting the 121-file subpath feature from v0.9.6 (`02dc3e68`) to v0.11.4
(`8bd8b4f`) was a **79-file conflict port**, done as a `git merge` (NOT
`git apply --3way`, which aborts the whole batch if any file was deleted
upstream). Conflicts were "merge both sides": keep the new upstream feature
code AND keep the subpath `${base}` prefix; `// LICENSE ...` branding comments
were preserved. Cost: ~1 agent-hour. Treat any future upstream bump as a
*porting task*, not a re-apply.

> **Tooling note:** when re-porting, prefer `git merge` of the previous
> subpath branch onto the new upstream over regenerating + re-applying the
> raw patch. `git apply --3way` requires `git diff --full-index` and still
> fails atomically on files that no longer exist in the new upstream.

## Copy-paste agent prompt

Give this to an agent (or yourself), substituting the version:

```
Upgrade deploy/openwebui-subpath to Open WebUI <NEW_VERSION>.

Facts:
- Current pinned base: 8bd8b4fac5e059578ac0c74b3c18d11139f88b7d (v0.11.4).
- Patch: deploy/openwebui-subpath/subpath.patch (185 files, the port of the
  spencercjh fork's subpath support onto v0.11.4; makes svelte/vite base,
  constants.ts WEBUI_BASE_URL, FastAPI root_path, socketio_path, redirects,
  and all goto()/href/location.href/static refs prefix-aware via
  WEBUI_SUBPATH). Old 121-file v0.9.6 patch kept as subpath-v0.9.6.patch.
- Second patch: 0002-node-build-heap.patch (NODE_OPTIONS 6144; SvelteKit
  build OOMs without it).
- Subpath must remain /chat.
- Evidence bar (all must pass before we call it done):
  1. `bash build.sh SHA=<new-sha> TAG=simpleaudit/open-webui-subpath:try` succeeds
     (build.sh gates on `git apply --check`; it must NOT force or fuzz-apply).
  2. Fresh container: GET /chat/health → {"status":true}.
  3. GET /chat/ → SPA HTML with ZERO unprefixed root refs
     (no `"/_app/`, `"/api/`, `"/auth"`, `"/static/` without the /chat prefix).
  4. WS upgrade GET /chat/ws/socket.io/?EIO=4&transport=websocket → 101.
  5. Sign in (admin/seed password), register a mock OpenAI key via
     POST /chat/openai/config/update (OPENAI_API_CONFIGS dict), send a message,
     and observe a streamed reply at /chat/c/<id>.
  6. Studio through Caddyfile.subpath: SSO sign-in still works, fallthrough to
     Studio for non-/chat paths still works.

Method:
1. Clone upstream fresh at <NEW_VERSION>; note the exact commit SHA.
2. `git apply --check subpath.patch`. Expect conflicts (see drift data in
   UPGRADE.md). For each conflicting file, re-derive the equivalent change on
   the new upstream code by reading what the patch does to that file — the
   subpath invariants to preserve are, in priority order:
   a. socketio_path = f'{WEBUI_SUBPATH}/ws/socket.io'   (raw path, not
      root_path-relative — engine.io matches raw; a strip-proxy breaks this)
   b. FastAPI(root_path=WEBUI_SUBPATH) + uvicorn.run(root_path=...) in BOTH
      serve and dev entrypoints
   c. svelte.config.js: paths.base=SUBPATH, relative:false
   d. vite.config.ts: base
   e. constants.ts: WEBUI_BASE_URL = base (single API choke point)
   f. redirects return WEBUI_SUBPATH-prefixed paths
   g. frontend: every goto()/href/location.href for internal routes prefixed
3. Regenerate subpath.patch = git diff <new-sha>..HEAD EXCLUDING fork
   infra: `git diff <new-sha>..HEAD -- ':!.github' ':!FORK.md'` (the deliverable
   patch is functional files only; .github/FORK.md belong to the fork repo).
   Update SHA default in build.sh; record the new pinned SHA in this file.
4. Run the evidence bar (1-6).
5. Update this file's drift table with the outcome.
6. Publish to the fork (so the tag image is pullable):
   - push the port to `main` (force-push — main = functional head + CI
     housekeeping commits appended on top):
     `git push -f fork HEAD:main`
   - make a PUBLISH branch = pristine upstream + fork CI plumbing ONLY:
     `git checkout -b publish-vNEW <new-sha>`
     `git checkout <main-tip> -- .github/workflows/docker.yaml .github/workflows/release-pypi.yml FORK.md`
     `git commit -am "ci: subpath-aware docker.yaml + fork infra (pristine base)"`
     `git push fork publish-vNEW:refs/heads/vNEW` (buildable branch)
     `git push -f fork vNEW` (tag = pristine sha)
   - dispatch the plain build:
     `gh workflow run docker.yaml -R sushantgautam/open-webui -r vX.Y.Z`
     (concurrency group is per-ref: pushing to main cancels main docker runs
     but never the tag/branch ones; the publish branch must carry the
     docker.yaml with the GHCR_TOKEN login fallback, or every push
     permission-denies on ghcr).
   - Publishing is **GHCR-only** — the upstream `copy-to-dockerhub` job is
     removed from the fork's docker.yaml and must stay removed.
   - The fork's docker.yaml is **dispatch-only** (no push trigger) with a
     `prune_only` input and an auto-`prune` job: every publish deletes stale
     package versions (keeps this run's `git-<sha7>[-variant][-subpath]`
     tags + pinned `v*` tags). To tidy without building:
     `gh workflow run docker.yaml -R sushantgautam/open-webui -r main -f prune_only=true`.
     Note: untagged "versions" in the GitHub packages UI are the per-arch
     leaf manifests of the multi-arch indexes.
     **NEVER delete untagged package versions** (via API or UI): on ghcr they
     are live constituents of the multi-arch indexes, and deleting them
     corrupts the indexes (observed 2026-10-04: 5 subpath indexes went 404
     after such a delete; recovered by rebuilding + re-aliasing). The prune
     job already handles this correctly.
   - Tag naming: the consumer tag family is `v<ver>-subpath[-variant]`
     (aliased from the build run's merge output via imagetools).
   - the subpath is BAKED at build time (svelte base + WEBUI_SUBPATH), so
     `-subpath` images must be built from a ref that carries the functional
     subpath code (e.g. `main`) — NOT the pristine publish branch:
     `gh workflow run docker.yaml -R sushantgautam/open-webui -r main -f webui_subpath=/chat`
     (tags get a `-subpath` suffix automatically, e.g. `main-subpath`).
     For a version-pinned `-subpath` image: build locally (build.sh, arm64)
     + `docker push`, then alias with
     `docker buildx imagetools create -t <registry>:<ver>-subpath
     <registry>:<ver>-<arch>` (what was done for v0.11.4-subpath).

If step 2's porting cost exceeds a day's work, STOP and report — do not
half-port; staying on the proven base is the fallback.
```

## Outcome log

| Date | Upstream | Result | Notes |
|------|----------|--------|-------|
| 2026-10-04 | v0.11.4 (`8bd8b4f`) | **not ported** | 33/121 clean, 88 conflicts (8/9 core; `svelte.config.js` was the one clean core file). Stayed on 02dc3e68 base. |
| 2026-10-04 | v0.11.4 (`8bd8b4f`) | **ported ✓** | Re-ran as a `git merge` of the 02dc3e68 subpath branch onto v0.11.4 (git-apply `--3way` aborts whole-batch on deleted files). 79 conflicts resolved merge-both: v0.11.4 features kept, `${base}` prefix applied; LICENSE branding comments preserved. Port commit `42ac2ae70` (123 files, +1017/−305). `build.sh` now pins `8bd8b4f`. Old 121-file patch kept as `subpath-v0.9.6.patch` for reference. |
| 2026-10-04 | v0.11.4 (`8bd8b4f`) | **published ✓** | E2E-verified in a real browser (chat → streamed LLM reply under `/chat/`, 7-point bar + screenshot). Fork `sushantgautam/open-webui`: `main` = functional head `8a71dc87d` (port + ruff + i18n + prettier), `dev` pristine, tag + branch `v0.11.4` = `8bd8b4f`. Final `subpath.patch` = `8bd8b4f..8a71dc87d` EXCLUDING `.github/`+`FORK.md` (4711 lines, 185 files), `git apply --check` clean on pristine. CI: fork GITHUB_TOKEN cannot push ghcr (→ `GHCR_TOKEN` secret fallback in docker.yaml); PyPI workflow manual-only (trusted publishing is upstream's); repo `actions=write`. Images: `ghcr.io/sushantgautam/open-webui:v0.11.4-arm64` + `:v0.11.4-subpath` (arm64, local build + imagetools); multi-arch `:v0.11.4` via CI tag dispatch. |
| | | | |
