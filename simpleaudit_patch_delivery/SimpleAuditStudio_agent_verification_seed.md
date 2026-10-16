# Seed prompt — SimpleAudit Studio agent-safe lifecycle verification

You are the lead coding and verification agent for `SimulaMet/SimpleAuditStudio`.
Read `AGENTS.md`, then examine the current `main` branch and the attached
`simpleaudit_agent_safe_lifecycle.patch`. My ChatGPT session could read the
repo but its GitHub connector returned **HTTP 403 on branch creation**, so
this patch has NOT been pushed. You must use your own authenticated Git checkout
and push a feature branch.

## Task

1. Create/switch to `feat/agent-safe-dev-lifecycle` from current `main` (do not
   overwrite another person's work). Run `git apply --check
   simpleaudit_agent_safe_lifecycle.patch`, review the changes, then apply the
   patch. If `main` advanced and the patch needs adjustment, reconcile it
   carefully; do not force-apply or overwrite unrelated changes.
2. Review and correct the implementation thoroughly, not just the unit tests.
   Keep both CLI surfaces minimal and consistent:
   `dev|spin [status|down|restart|reset|uninstall|help|agent-info]`.
   Preserve existing startup options. `agent-info` must work without starting
   Django, migrating a database, or claiming a port. `dev`/`spin` called twice
   must reuse the existing instance instead of restarting/killing it.
3. Worktrees must isolate Caddy's public port, Django internal port, Open WebUI
   port/data/managed venv, embedded Hatchet state, Django database, and optional
   pipelines. Do not silently connect different branches to one SQLite file.
   Verify one normal checkout plus TWO Git worktrees can run concurrently.
4. Validate all lifecycle transitions and recovery paths (normal exit,
   repeated start, SIGTERM, PID reuse, child crash, stale pid files, port
   takeover, same-port race, incompatible migrations, restarts). No command may
   kill an unrelated process or another worktree. Rewrite `devctl.sh` cleanly
   to remove all dead legacy kill-by-port code; do not merely bypass it.
5. Reload model: confirm Django Python autoreload and template refresh. The
   initial patch **does not implement worker-only hot reload**: `dev_server`
   currently runs Hatchet worker in the owning parent. Implement isolated
   worker reload only if it is safe with the SDK's embedded handshake and
   running audits, else explicitly report the limitation and propose a
   separate phase. Never promise independent worker restart while killing the
   entire stack instead.
6. For `reset` and `uninstall`, test only with throwaway state/data and mock
   uv executables. Confirm custom data locations, external DBs, symlinks,
   unrelated tools, shared caches and source trees cannot be deleted.
   `uninstall` should clean only Studio-related uv wheel cache when no other
   Studio instance needs it. Document that uvx temporary environments may
   survive package-specific `uv cache clean`.
7. Run formatting, `uv run ruff check .`, targeted lifecycle tests, existing
   relevant CLI tests, `uv run pytest -m 'not embedded_hatchet'`, and serial
   embedded-Hatchet tests where practical. Actually start full stack locally
   with no real commercial model API calls; inspect health and browser pages.
   Test `spin` as an installed wheel/uvx entry point, not only `dev`.
8. Update README, `AGENTS.md`, and developer lifecycle docs. Ensure every
   new command has useful `help`, human-readable errors, and --yes only for
   authorized destructive operations.
9. Commit the verified changes on `feat/agent-safe-dev-lifecycle`, push to
   `origin`, and provide the branch URL and concise test results. Do NOT merge.

## Mandatory safety checks

- Never use `pkill`, broad process-name matches, `kill -9`, or global
  `uv cache clean` to resolve conflicts.
- Never reset/uninstall real development, remote, shared, or production data.
  Use isolated temporary HOME / XDG_STATE_HOME and a throwaway checkout.
- Never run migrations automatically against shared PostgreSQL from parallel
  branches. Confirm mode/environment before starting Django.
- Do not pass worker reload as finished unless demonstrated with active jobs.
- Show exact owner PID, worktree identity, ports, and data directory in status.
- A failed integration test must block the push or be clearly labeled as
  unresolved if it is environmental; do not claim success based on stubs.

## Deliverable

A pushed feature branch with code, tests, user documentation, and a concise
report separating verified behavior from still-unimplemented capabilities.
