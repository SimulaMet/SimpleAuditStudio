# SimpleAudit Studio — Deployment

Status: current (matches the code)  
Date: 2026-09-28

Three ways to run it. All of them run `migrate` on start and create the admin
user and default workspace if missing.

| | Demo / HF Space | Local development | Docker Compose |
|---|---|---|---|
| Start | `uvx simpleaudit-studio` or the root `Dockerfile` | `uv run manage.py dev_server --embedded` | `docker compose up -d` |
| Database | SQLite | SQLite | PostgreSQL 16 |
| Queue | embedded Hatchet (in-process) | embedded Hatchet | `hatchet-server` container |
| Web server | `runserver` (port 7860 in the image, 8000 with uvx) | `runserver` :8000 | gunicorn :8000 |
| Persistent data | no (HF Space storage is ephemeral) | local files | Docker volumes |

## 1. Demo: uvx and the HF Space

```bash
uvx simpleaudit-studio          # models point at api.openai.com; add a key in the UI
uvx simpleaudit-studio --mock   # built-in mock model server, simulated results
```

`simpleaudit_studio/cli.py` sets `SIMPLEAUDIT_MINIMAL=1`, migrates, bootstraps,
seeds scenario packs and model connections, starts embedded Hatchet and runs the
web server and worker in one process.

The root `Dockerfile` builds the same thing for the Hugging Face Space
(`CMD python -m simpleaudit_studio.cli`, port 7860). It sets demo defaults
(`DEMO_MODE=true`, user `studio` / `admin123`); override secrets in the Space
settings. Data is lost when the Space restarts.

## 2. Local development

```bash
cp .env.local.example .env
uv sync --extra dev
uv run manage.py setup_local            # migrate + admin (BOOTSTRAP_PASSWORD from .env) + seed
uv run manage.py dev_server --embedded  # web + worker + embedded Hatchet → http://localhost:8000
SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra
```

`dev_server` applies migrations and bootstraps the admin (a superuser) and the
default workspace from `BOOTSTRAP_*` in `.env` every time it starts, like the
Compose `web` service and the uvx CLI. Only one `dev_server --embedded` can run at a time: they share the embedded
PostgreSQL directory (`~/.simpleaudit-studio/embedded-pg`, or
`SIMPLEAUDIT_EMBEDDED_PG_DIR`). The worker does not auto-reload; restart
`dev_server` after changing worker code.

## 3. Docker Compose (production)

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

## 4. Environment variables

Only these are read by the code.

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Required outside local SQLite mode. |
| `DJANGO_DEBUG` | `true` for debugging only. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated; default `*` (see security notes). |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Extra trusted origins (full URLs). |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_CONN_MAX_AGE` | PostgreSQL connection. |
| `SIMPLEAUDIT_LOCAL_SQLITE` | `1` = local SQLite database (`local_test.sqlite3`). |
| `SIMPLEAUDIT_MINIMAL` | `1` = single-process demo mode (set by the CLI). |
| `SIMPLEAUDIT_EMBEDDED_PG_DIR` | Data directory for embedded Hatchet's PostgreSQL. |
| `HATCHET_SERVER_URL`, `HATCHET_GRPC_URL`, `HATCHET_API_KEY`, `HATCHET_TOKEN_FILE`, `HATCHET_TLS_STRATEGY` | External Hatchet connection. |
| `HATCHET_EMBEDDED_HANDSHAKE` | Set internally by `dev_server --embedded` so web and worker find the embedded engine; don't set it yourself. |
| `WORKER_POOL` | Worker label (`cpu` by default). |
| `BOOTSTRAP_USERNAME`, `BOOTSTRAP_EMAIL`, `BOOTSTRAP_PASSWORD`, `BOOTSTRAP_PROJECT_NAME` | First admin user and workspace. |
| `DEMO_MODE`, `DEMO_USERNAME`, `DEMO_PASSWORD` | Prefilled demo login and the HF iframe cookie/CSRF policy. |
| `WORKOS_CLIENT_ID`, `WORKOS_API_KEY` | Optional passwordless email sign-in. |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT` | Optional error tracking. |
| `LOG_LEVEL`, `PORT` | Logging level; web port for the CLI. |
| any name in a connection's `secret_reference` | Model API key read at execution time. |

## 5. Management commands

| Command | Does |
|---|---|
| `setup_local` | migrate + bootstrap + seed (local dev). |
| `bootstrap_platform` | Create the admin user and default workspace (idempotent). |
| `seed_platform` | Import scenario packs and model connections, plus demo runs (`seed_demo_audits`). |
| `dev_server [--embedded]` | Web + worker for development. |
| `run_worker` | Hatchet worker (Compose `worker`). |
| `run_monitors` | One monitor pass, for an external cron if you don't run the worker sweeper. |
| `purge_test_data` | Delete runs, scenario sets and models left by smoke tests (`--dry-run` first). |

## 6. Operations

- **Health:** `/healthz` (liveness), `/readyz` (readiness), `/health/` (admin panel).
- **Backups:** `docker compose exec postgres pg_dump -U simpleaudit simpleaudit > backup.sql`. Restore with `psql` into an empty database before starting `web`.
- **Upgrades:** pull, `docker compose build`, `docker compose up -d`. Migrations run when `web` starts; take a backup first.

## 7. Security notes

- Set a strong `DJANGO_SECRET_KEY` and change `BOOTSTRAP_PASSWORD` (start-up
  checks reject empty values and `change-me`).
- `DJANGO_ALLOWED_HOSTS=*` is allowed because HF Spaces and similar proxies use
  unpredictable host names. Behind your own domain, list it explicitly.
- Direct API keys are stored on the connection in the database (never in run
  records or API responses); prefer `secret_reference` environment variables in
  production.
