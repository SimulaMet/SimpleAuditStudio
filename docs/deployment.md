# SimpleAudit Studio — Deployment

Status: current (matches the code)  
Date: 2026-10-03

The one reference for the **four standard ways** to run Studio. All four start
the same app at <http://localhost:8000> with the chat assistant on by
default, apply the database updates on start, and create the admin user and
default workspace if missing.

| # | Way | Standard command(s) | Database | Best for |
|---|-----|---------------------|----------|----------|
| 1 | **One-liner** | `uvx simpleaudit-studio@latest` | built in | trying it out; running a stable release |
| 2 | **Docker** (one container) | `docker build -t simpleaudit-studio .` then `docker run -d -p 8000:7860 -v sa-data:/data --name simpleaudit-studio simpleaudit-studio` | built in | self-hosting a single copy |
| 3 | **Compose** | `cp .env.example .env` (set three values) then `docker compose up -d` | PostgreSQL 16 (own container) | teams and production |
| 4 | **Development** | `uv run manage.py dev` | built in | working on the code in this repository |

"Ways" 1–3 need no database server: the app bundles a local SQLite file and
an embedded job-queue engine. Only Compose runs the full multi-container
stack with PostgreSQL.

Chat is on by default in every way and can always be turned off — with the
`--disable-chat` / `--no-chat` flag or `SIMPLEAUDIT_CHAT=off` (the flag beats
the env value, which beats the default). Each section below shows its toggle.

`uv run manage.py mode` prints which way is active on this machine and with
what settings. Older run setups (raw `dev_server` without the embedded
engine, external-Postgres development) are removed; the table above is the
whole surface.

## 1. One-liner (`uvx simpleaudit-studio`)

```bash
uvx simpleaudit-studio                    # models point at api.openai.com; add a key in the UI
uvx simpleaudit-studio --mock             # built-in mock model server, simulated results
uvx simpleaudit-studio --disable-chat     # start without the chat assistant
```

One process: migrates the database, creates the admin user and default
workspace, seeds scenario packs and model connections, starts the embedded
job queue, and runs the web app and worker.

Data lives in `~/.simpleaudit-studio/` (`SIMPLEAUDIT_DATA_DIR` moves it):
`studio.sqlite3` is the database, `embedded-pg/` the job queue. Back up that
folder to back up your work.

The same root `Dockerfile` also powers the hosted
[Hugging Face Space demo](https://sushantgautam-simpleaudit-studio.hf.space)
(demo login `studio` / `admin123`; override the secrets in the Space's
settings). The Space is disposable — its data is lost on restart.

## 2. Docker — one container

The root `Dockerfile` builds everything into a single container: web app,
worker, embedded job queue and chat, with all data in `/data`.

```bash
git clone https://github.com/SimulaMet/SimpleAuditStudio
cd SimpleAuditStudio
docker build -t simpleaudit-studio .
docker run -d -p 8000:7860 -v sa-data:/data --name simpleaudit-studio simpleaudit-studio
```

- The container serves on port **7860** (the Hugging Face Space convention);
  `-p 8000:7860` maps it to <http://localhost:8000> on your machine. Use
  `-p 7860:7860` if you'd rather keep the same number.
- All data (database, job queue, chat) lives in the `sa-data` volume, so the
  container can be deleted and recreated without losing anything. Without the
  `-v`, data is lost when the container is removed.
- Login is `studio` / `admin123`. For anything shared, add
  `-e BOOTSTRAP_PASSWORD=<strong-password>` to the `docker run` line (it only
  takes effect on first start, while the admin is created).
- Chat is on by default; add `-e SIMPLEAUDIT_CHAT=off` to run without it.
- Updating: rebuild the image, stop and remove the old container, run the
  same `docker run` line again — the `sa-data` volume carries over.

## 3. Compose (teams and production)

```bash
cp .env.example .env    # set POSTGRES_PASSWORD, BOOTSTRAP_PASSWORD, DJANGO_SECRET_KEY
docker compose up -d
docker compose --profile mock up -d   # also start the mock model server
```

The full stack, each piece in its own container (`docker-compose.yml`):

- `postgres` — the database; `deploy/postgres-init.sql` adds the queue's
  database on first start.
- `hatchet-server` — the job queue.
- `worker` — background jobs: audit runs, the stuck-run sweeper, monitor
  ticks.
- `web` — the app at <http://localhost:8000> (gunicorn). On start it applies
  the database updates, creates the admin and workspace, and seeds scenario
  packs (`SEED_ON_BOOT=false` skips seeding; `SEED_DEMO_AUDITS=false` keeps
  only the demo runs).
- `open-webui`, `chat-proxy` — the chat assistant at `/chat/` (on by
  default, below).
- `mock-model` — optional profile `mock`; a fake model API for testing.

Startup checks refuse to boot until `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`
and `BOOTSTRAP_PASSWORD` in `.env` are set to real values (empty or
`change-me` is rejected).

Your data lives in the named volumes `postgres_data` (database) and
`open_webui_data` (chat). `web` and `worker` build from
`deploy/compose/Dockerfile`.

Chat is on by default: `.env` ships with `SIMPLEAUDIT_CHAT=docker` and
`COMPOSE_PROFILES=chat`. To run Compose without chat, remove or comment out
both lines and run `docker compose up -d` again. (If your `.env` predates
chat being on by default, add those two lines to get it.)

## 4. Development (`uv run manage.py dev`)

The one standard way to run the app while working on this repository:

```bash
cp .env.local.example .env      # local settings (SQLite, local admin)
uv sync --extra dev             # install the app + dev tools, once
uv run manage.py setup_local    # first time only: create database + admin + sample data
uv run manage.py dev            # start: web + worker + queue + chat → http://localhost:8000
```

On every start it applies database updates, makes sure the admin user and
default workspace exist, and prints a one-time sign-in link, opening the
browser signed in (`--no-browser` skips the pop-up).

Day-to-day notes:

- Only one Studio can run at a time on a machine — the one-liner, the dev
  stack and the Docker container all use the same embedded job-queue folder.
- The web app hot-reloads on code changes; the background worker does not.
  After changing `infra/worker.py` or `infra/engine.py`, restart.
- Flags: `--disable-chat` (start without the chat assistant), `--no-worker`
  (web + API only), `--no-reload` (no auto-reload), `--port` (default 8000).
- Tests: `uv run pytest -n auto -m "not slow and not embedded_hatchet"` (fast
  set) or drop the marker filter for everything.

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
| `SIMPLEAUDIT_LOCAL_SQLITE` **internal** | `1` = local SQLite database at the repository's `local_test.sqlite3`. Set automatically by the `dev` entry point and by the test suite. |
| `SIMPLEAUDIT_MINIMAL` **internal** | `1` = single-process demo mode. Set by the CLI. |
| `SIMPLEAUDIT_CHAT` | Chat mode: `embedded` / `docker`, or `off` (and `disabled`, `false`, `no`, `0`, unset). On by default in every mode (`embedded` in the single-process modes, `docker` in compose — set in `.env`). Turn off with this var or the `--disable-chat` flag; the flag beats the env value (flag > env > mode default). |
| `SIMPLEAUDIT_DATA_DIR` | Where single-process mode keeps its data (default `~/.simpleaudit-studio`): the SQLite database and embedded Hatchet's PostgreSQL. |
| `SIMPLEAUDIT_EMBEDDED_PG_DIR` | Override just embedded Hatchet's PostgreSQL directory (default `<data dir>/embedded-pg`). |
| `HATCHET_SERVER_URL`, `HATCHET_GRPC_URL`, `HATCHET_API_KEY`, `HATCHET_TOKEN_FILE`, `HATCHET_TLS_STRATEGY` | External Hatchet connection (`compose` mode). |
| `HATCHET_EMBEDDED_HANDSHAKE` **internal** | Set by the entry points so the web app and worker find the embedded engine; don't set it yourself. |
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
| `dev_server` | Lower-level version of the same stack. Use `dev` instead. |
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
