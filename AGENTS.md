# AGENTS.md

SimpleAudit Studio — an audit platform for LLM systems. It wraps the external
SimpleAudit engine (Target → Auditor → Judge runs over versioned scenario sets)
with a Django web UI, a durable worker, and an optional Open WebUI chat module.

## Stack

- Python ≥3.11 (CI runs 3.12), Django 5.2, DRF + drf-spectacular
- PostgreSQL (canonical), SQLite (local dev + embedded mode)
- Hatchet durable job queue (embedded engine for single-process modes)
- uv for dependency management (see `uv.lock`)
- ruff (line length 88), pytest + pytest-django

## Run modes (read `config/runtime.py` before touching mode logic)

| mode | command | db | queue |
|---|---|---|---|
| dev | `manage.py dev` | SQLite (repo root, `dev.sqlite3`) | embedded |
| embedded | `spin` / `uvx simpleaudit-studio` | SQLite (`~/.simpleaudit-studio/`) | embedded |
| compose | `docker compose up -d` | Postgres | external Hatchet |
| single-docker | `docker run <image>` (HF Space) | SQLite (`/data`) | embedded |

Mode selection is env-flag driven and happens in `manage.py` / `simpleaudit_studio/cli.py`
**before** `django.setup()`. `config/runtime.py::resolve_mode()` is the single lens over
those flags; `config/settings.py` selects the DB engine from the same flags. Entry points
force flags (e.g. dev forces `SIMPLEAUDIT_LOCAL_SQLITE=1`) — don't fight that in `.env`.

## Development

```bash
cp .env.dev.example .env
uv sync --extra dev
uv run manage.py setup_local   # migrate + bootstrap admin + seed (idempotent)
uv run manage.py dev           # web + worker + embedded queue + chat
```

Login `studio` / `localdevpass123` (from `.env`). Migrations: `uv run manage.py makemigrations`.

## Tests

```bash
uv run pytest -m "not embedded_hatchet"          # full suite (parallel in CI via -n auto --testmon)
uv run pytest path/to/test_file.py -k "name"     # affected tests first
uv run pytest -m "embedded_hatchet"              # only if infra/worker.py or infra/minimal_config.py changed; serial only
uv run ruff check .
```

The suite runs on SQLite (`pytest.ini` sets the env). `@tag("slow")` and `@tag("embedded_hatchet")`
are mirrored to pytest markers by `conftest.py`. `e2e/test_ui.py` is a standalone Playwright
script, not collected by pytest.

## Repository layout

- `accounts/` — users, projects, auth (Django is the only authority on identity)
- `scenarios/`, `model_registry/`, `judges/`, `audits/` — domain apps; each keeps business
  logic in its `services.py`; models live in `<app>/models.py`, migrations in `<app>/migrations/`
- `infra/` — platform plumbing: `worker.py` (Hatchet tasks), `engine.py` (engine bridge),
  `tracing.py` (OTLP/Tempo/studio trace acquisition), `ui.py` (web views), `startup_checks.py`,
  `seed.py`; `infra/management/commands/` holds the run modes
- `chat/` — optional Open WebUI module; inert unless `SIMPLEAUDIT_CHAT` is set (`chat/config.py`)
- `simpleaudit_studio/` — the `spin` CLI entry point (`uvx` mode)
- `docs/` — `architecture.md`, `domain-model.md`, `deployment.md`, `chat.md`; link to these,
  don't duplicate them

## Hard boundaries (things that break subtly if violated)

- `infra/engine.py` is the **only** file that imports the `simpleaudit` engine, and it imports
  lazily inside functions. Never import the engine at module level elsewhere.
- The **web/API process never executes model calls** — only the worker (`infra/worker.py`).
  Audit progress is durable `AuditEvent` rows; SSE replays from them. Cancellation is a
  durable flag, not process memory.
- Scenarios execute from the **frozen endpoint snapshots** on the AuditRun, never from live
  registry rows. Do not "fix" a run by reading current model/agent configuration.
- Model secrets are stored as `secret_reference` identifiers and resolved from the environment
  at execution time. Raw credentials are never persisted.
- `infra/simpleaudit_package.py` resolves engine provenance from installed package metadata.
  Never hardcode engine versions/commits; to develop against an unmerged engine revision use the
  gitignored `simpleaudit-dependency.yaml` (`git_ref:`), not a `pyproject.toml` edit.
- Chat SSO is forward-auth: a proxy calls `GET /chat/authz` and injects `X-Studio-*` headers.
  Do not make anything read Django sessions/user tables outside Django.
- `config/settings.py` DB engine selection and `manage.py`/CLI flag pre-setting are load-order
  sensitive. Keep mode decisions before `django.setup()`.

## Coding conventions

- Thin views; business logic in the owning app's `services.py` (search for an existing helper
  before writing a new one).
- Type hints on new public functions; keep functions focused.
- Multi-row related writes: wrap in a transaction. Avoid N+1 (`select_related`/`prefetch_related`).
- Migrations: create per app, inspect the generated file, never edit already-applied migrations.
  Consider existing production (Postgres) data; avoid destructive changes.
- `RUF012` is intentionally ignored (Django Meta / factory-boy idioms) — don't "fix" those.

## Configuration files

- `.env.dev.example` → local dev only (`.env.example` → compose; the root `Dockerfile` bakes its
  own `ENV` for HF/single-container). `manage.py` auto-loads `.env`; real environment wins.
- `SIMPLEAUDIT_SQLITE_PATH` (dev-mode only) overrides the SQLite file location.
- Updating a setting means updating the relevant `.env*.example` **and** `docs/deployment.md`'s
  settings table.

## Definition of done

- Affected tests pass, then `uv run pytest -m "not embedded_hatchet"` and `uv run ruff check .`
- Migrations created/inspected if models changed
- Docs/env examples updated if configuration or externally visible behavior changed
- No unrelated reformatting or cleanup in the diff
- Summarize: what changed, design decisions, checks run, anything unresolved
