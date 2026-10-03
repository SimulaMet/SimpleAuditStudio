# SimpleAudit Studio — Deployment

Status: current (matches the code)  
Date: 2026-10-03

Four ways to run it. All of them run `migrate` on start and create the admin
user and default workspace if missing. Each mode is one command;
`uv run manage.py mode` prints the mode that is currently active and its
settings.

| | `dev` | `embedded` | `single-docker` | `compose` |
|---|---|---|---|---|
| Start | `uv run manage.py dev` | `uvx simpleaudit-studio` | `docker run -p 8000:8000 -v sa-data:/data <image>` | `docker compose up -d` |
| Process | single (hot-reload) | single | one container | multi-container |
| Database | SQLite | SQLite | SQLite | PostgreSQL 16 |
| Queue | embedded Hatchet | embedded Hatchet | embedded Hatchet | `hatchet-server` container |
| Chat | on | on | on | opt-in (profile) |
| Web server | `runserver` :8000 | `runserver` :8000 | `runserver` :8000 | gunicorn :8000 |
| Data | repo files + `.env` | `~/.simpleaudit-studio` | `/data` volume | Docker volumes |
| DEBUG | on | off | off | off |

The local modes (`dev`, `embedded`) share mechanics — SQLite + embedded
Hatchet + chat — and differ only in intent: `dev` runs your **source checkout**
with auto-reload and DEBUG; `embedded` runs the **installed artifact**
(`uvx`) with no reload.

## 1. Demo: uvx and the HF Space

```bash
uvx simpleaudit-studio          # models point at api.openai.com; add a key in the UI
uvx simpleaudit-studio --mock   # built-in mock model server, simulated results
```

`simpleaudit_studio/cli.py` sets `SIMPLEAUDIT_MINIMAL=1`, migrates, bootstraps,
seeds scenario packs and model connections, starts embedded Hatchet and runs the
web server and worker in one process.

Data lives in `~/.simpleaudit-studio/` (`SIMPLEAUDIT_DATA_DIR` changes it):
`studio.sqlite3` is the database and `embedded-pg/` is the embedded Hatchet
queue. It is outside the installed package, so upgrades and `uv cache clean`
keep it, and nothing is written to the folder you run from. Before 0.5.2 the
database lived inside the package, so each new version started empty; the
first start of 0.5.2 copies in the most recently used of those old databases
and says so. Back up that folder to back up your audits.

The root `Dockerfile` builds the same thing for the Hugging Face Space
(`CMD python -m simpleaudit_studio.cli`, port 7860). It sets demo defaults
(`DEMO_MODE=true`, user `studio` / `admin123`); override secrets in the Space
settings. Data is lost when the Space restarts.

## 2. Local development (`dev`)

```bash
cp .env.local.example .env
uv sync --extra dev
uv run manage.py setup_local            # migrate + admin (BOOTSTRAP_PASSWORD from .env) + seed
uv run manage.py dev                    # web + worker + embedded queue + chat → http://localhost:8000
SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra
```

`dev` is the named entry point for local development (single-process, SQLite,
embedded Hatchet, hot-reload, DEBUG, `.env`). It is a thin alias of
`dev_server --embedded`; the mode is pinned to `dev` automatically.

`dev` applies migrations and bootstraps the admin (a superuser) and the default
workspace from `BOOTSTRAP_*` in `.env` every time it starts, like the Compose
`web` service and the uvx CLI. On start it prints a one-time sign-in link
(`/auto-login/?token=...`, single use) and opens the browser signed in;
`--no-browser` skips the pop-up. Only one `dev` / `dev_server --embedded` /
`uvx` can run at a time: they share the embedded queue directory
(`~/.simpleaudit-studio/embedded-pg`, or `SIMPLEAUDIT_EMBEDDED_PG_DIR`). The
worker does not auto-reload; restart the dev server after changing worker code.

Chat is on by default; `uv run manage.py dev --disable-chat` (or
`SIMPLEAUDIT_CHAT=off`) turns it off. `uv run manage.py mode` prints the active
mode and its settings.

## 3. Single Docker (`single-docker`)

The root `Dockerfile` builds the `embedded`/HF-Space bundle into one container
(SQLite + embedded Hatchet + chat, single process). Point a named volume at
`/data` to persist it:

```bash
docker build -t simpleaudit-studio .
docker run -p 8000:8000 -v sa-data:/data simpleaudit-studio   # web → http://localhost:8000
```

(On Hugging Face Spaces the same image runs with `DEMO_MODE=true`, user
`studio` / `admin123`; override secrets in the Space settings. Without a
volume, data is lost when the container restarts.)

## 4. Docker Compose (production)

```bash
cp .env.example .env    # set POSTGRES_PASSWORD, BOOTSTRAP_PASSWORD, DJANGO_SECRET_KEY
docker compose up -d
docker compose --profile mock up -d   # also start the mock model server
```

Services (`docker-compose.yml`):

- `postgres`: domain database; `deploy/postgres-init.sql` creates the separate Hatchet database on first start.
- `hatchet-server`: durable workflow engine (HTTP :8888, gRPC :7077).
- `worker`: `manage.py run_worker`; runs audits, the stuck-run sweeper and monitor ticks.
- `web`: migrate, `bootstrap_platform`, `seed_platform` (skip with `SEED_ON_BOOT=false`, or only the demo runs with `SEED_DEMO_AUDITS=false`), `collectstatic`, gunicorn.
- `mock-model` (profile `mock`): OpenAI-compatible mock server (`deploy/mock_openai_server.py`).

`web` and `worker` build from `deploy/compose/Dockerfile`.

## 5. Environment variables

Only these are read by the code.

`SIMPLEAUDIT_MODE` is the single knob you set to pin the mode
(`single-docker`, `compose`, `embedded`, `dev`); the entry points set it for
you, and `manage.py mode` shows the resolved result. The flags marked
**internal** are set by the entry points / `SIMPLEAUDIT_MODE` — you only
override them when you deliberately need to.

| Variable | Purpose |
|---|---|
| `SIMPLEAUDIT_MODE` | Pin the run mode: `single-docker`, `compose`, `embedded`, `dev`. See `manage.py mode`. |
| `DJANGO_SECRET_KEY` | Required outside local SQLite mode. |
| `DJANGO_DEBUG` | `true` for debugging only. `dev` sets it; the others leave it off. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated; default `*` (see security notes). |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Extra trusted origins (full URLs). |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_CONN_MAX_AGE` | PostgreSQL connection (`compose` mode). |
| `SIMPLEAUDIT_LOCAL_SQLITE` **internal** | `1` = local SQLite database (`local_test.sqlite3`). Set by the single-process modes. |
| `SIMPLEAUDIT_MINIMAL` **internal** | `1` = single-process demo mode. Set by the CLI. |
| `SIMPLEAUDIT_CHAT` | Chat mode: `embedded` / `docker`, or `off` (and `disabled`, `false`, `no`, `0`, unset). On by default in single-process modes. Turn off with this var or the `--disable-chat` flag; the flag beats the env value (flag > env > mode default). |
| `SIMPLEAUDIT_DATA_DIR` | Where single-process mode keeps its data (default `~/.simpleaudit-studio`): the SQLite database and embedded Hatchet's PostgreSQL. |
| `SIMPLEAUDIT_EMBEDDED_PG_DIR` | Override just embedded Hatchet's PostgreSQL directory (default `<data dir>/embedded-pg`). |
| `HATCHET_SERVER_URL`, `HATCHET_GRPC_URL`, `HATCHET_API_KEY`, `HATCHET_TOKEN_FILE`, `HATCHET_TLS_STRATEGY` | External Hatchet connection (`compose` mode). |
| `HATCHET_EMBEDDED_HANDSHAKE` **internal** | Set by `dev` / `dev_server --embedded` so web and worker find the embedded engine; don't set it yourself. |
| `WORKER_POOL` | Worker label (`cpu` by default). |
| `BOOTSTRAP_USERNAME`, `BOOTSTRAP_EMAIL`, `BOOTSTRAP_PASSWORD`, `BOOTSTRAP_PROJECT_NAME` | First admin user and workspace. |
| `DEMO_MODE`, `DEMO_USERNAME`, `DEMO_PASSWORD` | Prefilled demo login and the HF iframe cookie/CSRF policy. |
| `WORKOS_CLIENT_ID`, `WORKOS_API_KEY` | Optional passwordless email sign-in. |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT` | Optional error tracking. |
| `LOG_LEVEL`, `PORT` | Logging level; web port for the CLI. |
| any name in a connection's `secret_reference` | Model API key read at execution time. |

## 6. Management commands

| Command | Does |
|---|---|
| `setup_local` | migrate + bootstrap + seed (local dev, one-shot). |
| `bootstrap_platform` | Create the admin user and default workspace (idempotent). |
| `seed_platform` | Import scenario packs and model connections, plus demo runs (`seed_demo_audits`). |
| `dev` | Local dev stack (embedded, hot-reload, chat, one-time sign-in). `--disable-chat`, `--no-worker`, `--no-reload`, `--no-browser`. |
| `dev_server [--embedded]` | Same stack, spelled out. Requires `--embedded` or `SIMPLEAUDIT_MODE`; the legacy external-Postgres path is removed. |
| `mode` | Print the resolved run mode and its settings. |
| `run_worker` | Hatchet worker (Compose `worker`). |
| `run_monitors` | One monitor pass, for an external cron if you don't run the worker sweeper. |
| `purge_test_data` | Delete runs, scenario sets and models left by smoke tests (`--dry-run` first). |

## 7. Operations

- **Health:** `/healthz` (liveness), `/readyz` (readiness), `/health/` (admin panel).
- **Backups:** `docker compose exec postgres pg_dump -U simpleaudit simpleaudit > backup.sql`. Restore with `psql` into an empty database before starting `web`.
- **Upgrades:** pull, `docker compose build`, `docker compose up -d`. Migrations run when `web` starts; take a backup first.

## 8. Security notes

- Set a strong `DJANGO_SECRET_KEY` and change `BOOTSTRAP_PASSWORD` (start-up
  checks reject empty values and `change-me`).
- `DJANGO_ALLOWED_HOSTS=*` is allowed because HF Spaces and similar proxies use
  unpredictable host names. Behind your own domain, list it explicitly.
- Direct API keys are stored on the connection in the database (never in run
  records or API responses); prefer `secret_reference` environment variables in
  production.
