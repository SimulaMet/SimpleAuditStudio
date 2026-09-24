# SimpleAudit Studio — Deployment

Status: current  
Date: 2026-09-22

## 1. Deployment goals

A new user should be able to deploy a functional self-hosted instance with minimal manual steps:

```bash
git clone <repository>
cd simpleaudit-studio
cp .env.example .env
# edit required secrets/settings
docker compose up -d
```

After startup, the user should get:

- web UI available at configured URL
- PostgreSQL initialized/migrated
- object storage bucket initialized
- workflow server available to workers
- CPU worker running
- optional GPU worker running if hardware/config present
- health checks passing
- documented first-run bootstrap command

No undocumented manual database setup is allowed.

First-run bootstrap (idempotent, runs automatically on container start):

1. create the platform owner account (`BOOTSTRAP_USERNAME`, default `studio`) with `is_staff=True` — not a Django superuser
2. create the Default workspace
3. grant the owner an ADMIN membership in the Default workspace
4. optionally import a starter scenario pack into that project
5. print or log next steps for model endpoint registration

Configuration via environment variables: `BOOTSTRAP_USERNAME`, `BOOTSTRAP_EMAIL`, `BOOTSTRAP_PASSWORD`, `BOOTSTRAP_PROJECT_NAME`.

Bootstrap must be idempotent and must not require hand-editing SQL.

## 2. Canonical components

| Component | Purpose | Default Compose service |
|---|---|---|
| Django web/API | UI + REST + SSE | `web` |
| PostgreSQL | authoritative state | `postgres` |
| MinIO/S3 | artifacts | `minio` |
| Hatchet server | durable workflow/queue | `hatchet-server` |
| CPU worker | runs audits on CPU/API models | `worker-cpu` |
| GPU worker | runs local/GPU inference | `worker-gpu` optional |
| OTel collector | traces/logs/metrics | optional |
| Langfuse | LLM observability | optional |

## 3. Environments

### 3.1 Development

Purpose:

- fast local iteration
- deterministic mock provider
- lower timeouts

Requirements:

- same component types as production where practical
- Docker Compose preferred
- no SQLite in canonical dev path
- seed command idempotent

### 3.2 Staging

Purpose:

- pre-release validation
- live provider smoke tests
- failure injection
- backup/restore drills

Requirements:

- isolated data
- same migration path as production
- access to representative GPU/API endpoints if available

### 3.3 Production

Purpose:

- real audits
- durable historical records
- controlled upgrades

Requirements:

- TLS termination
- strong secrets
- backups
- monitoring/alerting
- logged administrative actions
- pinned images
- documented rollback

## 4. Configuration

Configuration is split into:

### 4.1 Non-secret settings

`.env` or environment:

- `DJANGO_SETTINGS_MODULE`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `SIMPLEAUDIT_LOCAL_SQLITE` — local-dev/test only; forces the SQLite branch (never in deployments)
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `MINIO_ENDPOINT`
- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`
- `MINIO_BUCKET`
- `HATCHET_SERVER_URL`
- `HATCHET_GRPC_URL`
- `HATCHET_API_KEY`
- `HATCHET_TOKEN_FILE`
- `HATCHET_TLS_STRATEGY`
- `WORKER_POOL`
- `MAX_CONCURRENT_AUDITS`
- `MAX_SCENARIOS_PER_RUN` — single source of truth for scenario count validation
- `SSE_MAX_CONNECTIONS_PER_USER`
- `OTEL_EXPORTER_OTLP_ENDPOINT`
- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_HOST`

### 4.2 Secrets

Never commit:

- Django secret key
- Postgres password
- MinIO keys
- Hatchet API key
- model API keys
- Langfuse secret key
- signing keys

Secrets are injected into worker environment only when needed. Web/API should not receive model API keys unless an admin feature requires read-only validation.

Startup secret validation:

- refuse to boot in production if required values remain `change-me`
- refuse to boot if `DJANGO_DEBUG=true` with public allowed hosts
- the worker resolves SimpleAudit provenance from installed package metadata at
  startup; a run whose frozen version disagrees with the loaded engine fails with
  `SIMPLEAUDIT_VERSION_MISMATCH` (see `infra/simpleaudit_package.py`)
- validate database/object storage/workflow connectivity during readiness checks

### 4.3 `.env.example`

Must include placeholders and comments:

```env
DJANGO_SECRET_KEY=change-me
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

POSTGRES_DB=simpleaudit
POSTGRES_USER=simpleaudit
POSTGRES_PASSWORD=change-me
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

MINIO_ENDPOINT=http://minio:9000
MINIO_ACCESS_KEY=change-me
MINIO_SECRET_KEY=change-me
MINIO_BUCKET=simpleaudit-artifacts

HATCHET_SERVER_URL=http://hatchet-server:8888
HATCHET_GRPC_URL=hatchet-server:7077
HATCHET_API_KEY=

WORKER_POOL=cpu
MAX_CONCURRENT_AUDITS=2
MAX_SCENARIOS_PER_RUN=500

# SimpleAudit engine: no env vars needed. It is a pip dependency pinned in
# requirements.txt; provenance is read from installed package metadata.

# Optional observability
OTEL_EXPORTER_OTLP_ENDPOINT=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=
```

## 5. Dockerfiles

Recommended images:

- `web`: Python + Django + frontend assets if built
- `worker`: Python + SimpleAudit + worker dependencies
- `postgres`: official PostgreSQL image
- `minio`: official MinIO image
- `hatchet-server`: vendor image or pinned build
- optional `collector`: OpenTelemetry Collector

Rules:

- pin base images by digest or exact tag
- use non-root user where supported
- do not copy `.env` into image
- do not bake secrets into layers
- include healthcheck
- record application version and SimpleAudit commit in `/app/version.json` or equivalent

The self-hosting deployment uses one image:

- **`deploy/compose/Dockerfile`** — the compose-stack application image
  (web + worker only; Postgres/Hatchet run as separate containers). Referenced
  by `docker-compose.yml`, which builds it from the cloned checkout.

> Note: the repository also contains a root `Dockerfile` (a minimal-config
> single-process image serving :7860). It exists solely so Hugging Face Spaces
> can build the project (HF Spaces build only the root Dockerfile) and is not a
> documented self-hosting path.

## 6. Migrations

Startup sequence:

1. wait for PostgreSQL
2. run Django migrations
3. initialize object storage bucket if missing
4. verify workflow connectivity
5. start web server

Commands:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py check
docker compose exec web python manage.py collectstatic --noinput
```

Automatic migrations on web startup may be enabled for small deployments but must be configurable. Upgrades should document explicit migration step.

## 7. Health checks

Required endpoints:

- `/healthz` — liveness
- `/readyz` — readiness including DB/workflow/storage checks

Compose healthchecks:

- postgres: `pg_isready`
- minio: `mc ready` or HTTP health
- hatchet: vendor health endpoint
- web: `/healthz`
- worker: heartbeat/metrics endpoint or process supervision

Readiness must fail if:

- DB unreachable
- migrations pending
- object storage unreachable
- workflow server unreachable for submission

## 8. Logging

All containers emit structured JSON logs to stdout/stderr.

Correlation fields:

- `request_id`
- `user_id`
- `project_id`
- `audit_run_id`
- `workflow_run_id`
- `task_id`
- `trace_id`

Redaction:

- API keys
- bearer tokens
- full prompts/transcripts unless debug mode explicitly enabled
- cookie/session values

Log levels:

- production default: `INFO`
- debugging: `DEBUG` with explicit warning that transcripts may appear

## 9. Backups and restore

Back up:

- PostgreSQL logical dump or basebackup
- MinIO bucket
- optional workflow database if vendor requires
- `.env` stored securely outside repository

Restore test:

1. take backup
2. destroy disposable environment
3. restore DB and objects
4. run migrations
5. verify historical audit manifest and artifact hash
6. verify UI can open restored audit

Backup frequency and retention are deployment-specific but must be documented.

## 10. Upgrades

Upgrade procedure:

1. announce maintenance if needed
2. drain/cancel or allow running audits according to policy
3. pull new images
4. run migrations
5. restart web
6. restart workers
7. verify health checks
8. run smoke audit with mock provider

Rollback:

- keep previous image tags available
- do not use destructive migrations without forward-only plan or backup
- document incompatible schema changes
- prefer additive migrations

## 11. Deployment profiles

### Full profile (default)

Components:

- PostgreSQL
- MinIO/S3
- Hatchet server
- CPU worker
- optional GPU worker
- web/API
- optional OTel collector/Langfuse

Use for production and staging.

### Minimal profile (operator option)

Components:

- PostgreSQL
- Hatchet server
- CPU worker
- web/API
- local volume or managed object storage compatible with the same artifact interface

Langfuse/OTel collector may be omitted. This profile is an operational
simplification, not a second architecture. It must still use durable workflow,
PostgreSQL, authentication, and object-storage-compatible artifact persistence.

## 12. Scaling

Single host:

- scale worker concurrency within host resources
- run GPU worker only if GPU available

Multi host:

- remote workers point to same Postgres/MinIO/Hatchet
- add labels for capabilities
- no web server access to GPU required
- consider separate network segmentation for workers

Do not scale horizontally until single-host bottlenecks are measured.

## 13. Security deployment checklist

- TLS at reverse proxy
- strong unique secrets
- private object storage
- no public MinIO console unless protected
- CSRF trusted origins configured
- rate limiting enabled
- dependency scanning in CI
- container images scanned
- workers run non-root
- secrets not in image/env files committed to git
- SSRF policy configured for model endpoints
- audit logging enabled

## 14. Release process

Release candidate:

1. tag version
2. build images
3. run full CI
4. deploy clean staging
5. run E2E critical journey
6. run backup/restore drill
7. publish release notes
8. promote to production

Release notes include:

- version
- SimpleAudit commit
- migration summary
- breaking changes
- known issues
- upgrade instructions
- rollback instructions
