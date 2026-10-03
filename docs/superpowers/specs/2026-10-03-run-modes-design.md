# Run Modes — a single, unambiguous configuration model

Status: design (not yet implemented)
Date: 2026-10-03

## Problem

SimpleAudit Studio currently runs from four entry points (`uvx
simpleaudit-studio`, the root `Dockerfile` for HF Spaces,
`manage.py dev_server [--embedded]`, and `docker compose up`). The selection
of *how* the process runs is scattered across three overlapping environment
flags — `SIMPLEAUDIT_MINIMAL`, `SIMPLEAUDIT_LOCAL_SQLITE`,
`SIMPLEAUDIT_CHAT` — plus per-entry-point hard-coded assumptions. The result is
that two entry points which are "the same thing" behave differently, and a
user cannot state, in one place, *which mode* they are in.

Concrete symptom that motivated this design: the bundled chat (Open WebUI) is
**on by default** for `uvx simpleaudit-studio` but **off by default** for
`manage.py dev_server --embedded`. A developer who followed the documented
"local development" flow hit the
"Chat (Open WebUI) is not enabled on this server … Enable it with
`SIMPLEAUDIT_CHAT=embedded`" message with no obvious reason.

## Goals

1. Exactly **four** first-class run modes, each addressable as one documented
   command, with no ambiguity about DB, queue, chat, or data location.
2. A **single source of truth** for "which mode am I in", readable at runtime.
3. **Uniform defaults** for the single-process modes (notably: chat on).
4. Low-risk, incremental change: keep the existing entry points and env flags
   working; do not re-platform.

## Non-goals

- No new top-level CLI (`studio`) replacing `manage.py`.
- No renames of existing public env vars.
- No change to the Postgres/Hatchet behavior of Compose beyond the mode
  label.

## The four modes

| # | Mode ID | Command | Process model | DB | Queue | Chat | Data location | DEBUG |
|---|---------|---------|---------------|----|-------|------|---------------|-------|
| 1 | `single-docker` | `docker run -p 8000:8000 -v sa-data:/data <image>` | one container | SQLite | embedded Hatchet | on | `/data` (volume) | off |
| 2 | `compose` | `docker compose up -d` | multi-container | PostgreSQL 16 | `hatchet-server` container | opt-in (profile) | Docker volumes | off |
| 3 | `embedded` | `uvx simpleaudit-studio` | one process | SQLite | embedded Hatchet | on | `~/.simpleaudit-studio` | off |
| 4 | `dev` | `uv run manage.py dev` | one process (hot-reload) | SQLite | embedded Hatchet | on | repo-local (`local_test.sqlite3`) + `.env` | on |

`dev` is defined as **`embedded` plus**: source-checkout execution with
auto-reload, `DJANGO_DEBUG=1`, `.env` loading, and repo-local data. It is a
distinct name from `embedded` because the intent differs (iterate on code vs
run the installed artifact), even though the DB/queue/chat mechanics match.

## Design

### 1. `SIMPLEAUDIT_MODE` — the explicit mode knob

New environment variable `SIMPLEAUDIT_MODE`, one of
`single-docker | compose | embedded | dev`.

- It is the **one variable a user sets** to pin the mode.
- Every entry point sets a sensible default for it before Django reads
  settings, so it is always defined:
  - root `Dockerfile` entrypoint → `single-docker`
  - `docker-compose.yml` `web`/`worker` services → `compose`
  - `uvx` CLI (`simpleaudit_studio/cli.py`) → `embedded`
  - `manage.py dev` → `dev`
  - `manage.py dev_server --embedded` → `dev` (keeps the existing flag working,
    now labeled `dev`)
  - `manage.py dev_server` (no `--embedded`) → resolved from `SIMPLEAUDIT_CHAT`
    and queue config (legacy path, still supported).

### 2. `resolve_mode()` — single source of truth

A helper `resolve_mode() -> ModeProfile` in `config/` (e.g.
`config/runtime.py`) returns a small frozen dataclass:

```python
@dataclass(frozen=True)
class ModeProfile:
    id: str                 # "single-docker" | "compose" | "embedded" | "dev"
    process: str            # "single" | "multi-container"
    database: str           # "sqlite" | "postgres"
    queue: str              # "embedded" | "external"
    chat: str               # "on" | "off" | "opt-in"
    debug: bool
```

Resolution order: explicit `SIMPLEAUDIT_MODE` wins; otherwise infer from the
existing signals (`SIMPLEAUDIT_MINIMAL`, presence of embedded queue handshake,
Compose service markers). The existing flags become **derived inputs**, not the
thing the user reasons about. `config/settings.py` continues to branch on the
flags it does today, but reads them from the resolved profile where practical
so there is one place the mapping lives.

This is deliberately additive: nothing that reads
`SIMPLEAUDIT_LOCAL_SQLITE` / `SIMPLEAUDIT_MINIMAL` / `SIMPLEAUDIT_CHAT`
changes behavior. `resolve_mode()` is a *lens* over those flags, not a
replacement, which keeps the change reviewable and low-risk.

### 3. Uniform chat default

Chat (Open WebUI) is **on by default for every single-process mode**
(`embedded`, `dev`, `single-docker`), and stays **opt-in for `compose`**
(chat is a separate container there, gated by `COMPOSE_PROFILES=chat`).

Implementation: `manage.py dev_server` sets
`os.environ.setdefault("SIMPLEAUDIT_CHAT", "embedded")` the same way the CLI
already does. This is the direct fix for the motivating symptom.

### 4. `manage.py dev` — the missing honest name

New management command `infra/management/commands/dev.py`: a thin alias of
`dev_server` that (a) defaults to `--embedded`, (b) sets
`SIMPLEAUDIT_MODE=dev`, and (c) delegates to `dev_server.handle`. Existing
`dev_server` invocations are unchanged. This gives mode 4 the name the docs
will use without inventing mechanics.

### 5. `manage.py mode` — the "what am I running" diagnostic

New command that prints the resolved profile:

```
$ uv run manage.py dev
# or, in a running process:
$ uv run manage.py mode
Mode:      dev
Process:   single (hot-reload)
Database:  sqlite (/abs/path/local_test.sqlite3)
Queue:     embedded (grpc 127.0.0.1:54638)
Chat:      on (proxy http://localhost:8801)
DEBUG:     on
Data dir:  /abs/path  (or ~/.simpleaudit-studio)
```

When behavior looks wrong, one command reveals exactly what is active.

### 6. Documentation

One authoritative table in both `README.md` (the "Daily work" / "Other
setups" area) and `docs/deployment.md` (replace the current 3-column
"Demo / Local development / Docker Compose" table with the 4-mode table above,
one row per mode, one command each). The env-var reference keeps the existing
variables but marks `SIMPLEAUDIT_MINIMAL`, `SIMPLEAUDIT_LOCAL_SQLITE`, and
`SIMPLEAUDIT_CHAT` as **"set by the entry points / `SIMPLEAUDIT_MODE`; you only
set them to override"**. The chat line notes it is on by default in
single-process modes.

## Error handling / edge cases

- **Mode conflict:** if a user sets `SIMPLEAUDIT_MODE=dev` but the resolved
  DB would be Postgres (e.g. a `.env` with Postgres creds and no
  `SIMPLEAUDIT_LOCAL_SQLITE`), `resolve_mode()` prefers the explicit mode and
  the `dev`/`embedded` paths force the SQLite branch, with a one-line startup
  warning noting the override. This matches the existing
  `dev_server --embedded` behavior (SQLite implied).
- **Only one `--embedded`/`dev`/`uvx` at a time:** unchanged — they share the
  embedded Postgres dir; the existing startup notice stays.
- **Legacy `dev_server` (no `--embedded`) against external Postgres +
  Hatchet:** still supported; resolves to a profile with
  `database=postgres, queue=external`. Not given a new name; documented as
  "advanced".
- **Unknown `SIMPLEAUDIT_MODE` value:** startup error listing the four valid
  values (fail fast, don't silently guess).

## Testing

- `resolve_mode()`: unit tests asserting each (flags/env) combination resolves
  to the expected `ModeProfile`; explicit `SIMPLEAUDIT_MODE` overrides
  inference; unknown value raises.
- Chat default: assert `dev_server` sets `SIMPLEAUDIT_CHAT` to `embedded`
  when unset (mirrors the existing CLI test).
- `manage.py dev`: assert it is accepted and delegates (smoke: `--help` and a
  resolve check), and that `dev_server` behavior is unchanged.
- `manage.py mode`: assert it renders the profile and exits 0 for each of the
  four modes (fixture env vars).
- No new `slow` / `embedded_hatchet` tags needed; these are lightweight.

## Out of scope (deferred)

- A `studio` console script with subcommands.
- Auto-upgrading `docker-compose.yml` profiles.
- Persisting the mode to disk or a config file (env var is enough).

## Rollout order (for the later implementation plan)

1. `resolve_mode()` + `ModeProfile` + unit tests (no behavior change).
2. Uniform chat default in `dev_server`.
3. `manage.py dev` alias.
4. `manage.py mode` diagnostic.
5. Docs: 4-mode table in README + deployment.md, mark flags as internal.
