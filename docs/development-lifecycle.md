# Agent-safe local development and `spin`

`dev` (source checkout) and `spin` (installed / uvx) share the lifecycle manager.
It identifies instances by canonical checkout path (`dev`) or data directory
(`spin`). Calling the entry point twice **does not** restart a healthy stack.

```sh
uv run manage.py dev                 # start/reuse Django, Hatchet, worker, Caddy, Open WebUI
uv run manage.py dev status          # owner, health and allocated public port
uv run manage.py dev agent-info      # exhaustive instructions for agents
uv run manage.py dev down            # graceful shutdown; preserve data
uv run manage.py dev restart         # intentional full restart
uv run manage.py dev reset           # destructive; reseed and start
uv run manage.py dev uninstall       # remove owned data + scoped uv caches

uvx --from simpleaudit-studio spin status
uvx --from simpleaudit-studio spin agent-info
uvx --from simpleaudit-studio spin down
uvx --from simpleaudit-studio spin reset
uvx --from simpleaudit-studio spin uninstall
```

`reset` and `uninstall` confirm interactively; use `--yes` for explicitly
authorized noninteractive runs. `uninstall` removes managed Open WebUI state,
embedded Hatchet data, local databases, and package-scoped `uv` wheel caches.
It does **not** remove source, external result folders, shared Postgres, unrelated
uv artifacts, system Python, or a source checkout's `.venv`. `uvx` may retain
shared cached environments that cannot be safely identified by package.

## Parallel worktrees

A linked Git worktree gets isolated ports, chat storage, embedded Hatchet
PostgreSQL and Django SQLite by default. The instance manager allocates ports
before Django settings or migration start. To find the actual URL, use `status`;
never assume port 8000. The public `--port` flag also configures Caddy's public
listener, Django internal port, and the Open WebUI upstream.

Data layout:

- `dev`: checkout's `dev.sqlite3`; other services under
  `~/.local/state/simpleaudit-studio/data/dev-<hash>/`
- `spin`: `~/.simpleaudit-studio/` (compatible with existing installations)
- Registry: `${XDG_STATE_HOME:-~/.local/state}/simpleaudit-studio/instances/`

The manager refuses to destructively delete custom `SIMPLEAUDIT_DATA_DIR` or
`SIMPLEAUDIT_SQLITE_PATH` locations and never kills a PID merely because it
happens to use a preferred port. A second process sharing the same data dir is
rejected while another registered owner is alive.

## Reload behavior and agent policy

Django's existing `runserver` child autoreloads Python code. Edited Django
templates are read again on refresh; the browser is not automatically refreshed
without a separate browser-reload integration. **The embedded Hatchet worker
currently remains inside the owning parent process and is not automatically
reloaded**. Changes to worker code require a planned restart, preferably while
no audits are running. Do not pretend a Django reload updates the worker.

Always check `dev agent-info`/`spin agent-info` and `status` before attempting to
start, restart, reset, or uninstall. Never manually kill a port holder, change a
running instance's persistent data, or restart an unrelated worktree. `reset`
and `uninstall` require the user to authorize potential data loss.

## Scope and caveats

This manager supervises the **existing single-process embedded stack**; it does
not magically turn Hatchet's worker into an independently hot-reloadable process
or enable shared PostgreSQL for `dev`. Shared-database/multi-instance scenarios
need a separate Postgres-backed development mode and migration coordination.

`devctl.sh` is kept as a compatibility wrapper and must not use its former
`kill -9`/`pgrep` port-based cleanup. Running `uvx` itself may download the CLI
package again before an `uninstall` subcommand is processed; uninstalling a
uv-managed permanent tool is a distinct operation handled only if it exists.
