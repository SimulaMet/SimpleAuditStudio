# =============================================================================
# SimpleAudit Studio — single-container image (Hugging Face Space)
#
# THIS is the root Dockerfile, which is what Hugging Face Spaces build (HF only
# builds the root Dockerfile; there is no frontmatter key to point elsewhere).
# It packs the entire stack (Postgres 16, Hatchet server, SimpleAudit worker,
# Django web) into one container managed by supervisord, serving :7860.
#
# The docker-compose application image (web + worker only, no DB/queue) lives at
# deploy/compose/Dockerfile and is referenced by docker-compose.yml / CI.
#
# Build:  docker build -t simpleaudit-studio .
# Run:    docker run -p 7860:7860 simpleaudit-studio
# =============================================================================

# --- Stage 1: Extract Hatchet binaries ----------------------------------------
FROM ghcr.io/hatchet-dev/hatchet/hatchet-lite-dev:latest AS hatchet-src

# --- Stage 2: Main application -------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

# --- System dependencies -----------------------------------------------------
# postgresql: local DB for both SimpleAudit domain and Hatchet queue
# supervisor: process manager (replaces docker-compose orchestration)
# curl: healthchecks
# git: required to install SimpleAudit engine from its pinned git ref
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql \
        postgresql-client \
        supervisor \
        sudo \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# --- Python dependencies -----------------------------------------------------
COPY requirements.txt .
RUN pip install -r requirements.txt

# --- Application code --------------------------------------------------------
COPY . .

# Collect static files so Django can serve them without DEBUG.
RUN python manage.py collectstatic --noinput

# --- Hatchet binaries + static assets -------------------------------------------
COPY --from=hatchet-src /hatchet-lite /usr/local/bin/hatchet-lite
COPY --from=hatchet-src /hatchet-migrate /usr/local/bin/hatchet-migrate
COPY --from=hatchet-src /hatchet-admin /usr/local/bin/hatchet-admin
COPY --from=hatchet-src /static-assets /static-assets
RUN chmod +x /usr/local/bin/hatchet-lite /usr/local/bin/hatchet-migrate /usr/local/bin/hatchet-admin

# --- Postgres initialization --------------------------------------------------
# Create data directory owned by postgres user. The startup script will
# initdb on first run (ephemeral storage means every start is "first run").
# Grant appuser passwordless sudo for postgres commands (su requires root).
RUN mkdir -p /var/lib/postgresql/data /var/run/postgresql /etc/sudoers.d \
    && chown -R postgres:postgres /var/lib/postgresql /var/run/postgresql \
    && printf 'appuser ALL=(postgres) NOPASSWD: ALL\n' > /etc/sudoers.d/appuser-postgres \
    && chmod 440 /etc/sudoers.d/appuser-postgres

# --- Supervisord config -------------------------------------------------------
COPY deploy/hf-space/supervisord.conf /etc/supervisor/conf.d/simpleaudit.conf
COPY deploy/hf-space/start.sh /app/start.sh
RUN chmod +x /app/start.sh

# --- Non-root app user (Postgres runs as postgres via supervisord) ------------
RUN useradd --create-home appuser \
    && mkdir -p /app/staticfiles /app/media /var/log/supervisor \
    && chown -R appuser:appuser /app /var/log/supervisor
USER appuser

# --- Environment defaults for the Space --------------------------------------
# These are safe demo defaults. Override via HF Space Secrets for anything
# sensitive. POSTGRES_HOST=localhost because everything is in one container.
ENV POSTGRES_HOST=localhost \
    POSTGRES_PORT=5432 \
    POSTGRES_DB=simpleaudit \
    POSTGRES_USER=simpleaudit \
    POSTGRES_PASSWORD=simpleaudit_hf_demo \
    HATCHET_SERVER_URL=http://localhost:8888 \
    HATCHET_GRPC_URL=localhost:7077 \
    HATCHET_TOKEN_FILE=/app/hatchet-config/authdisabled-token \
    HATCHET_TLS_STRATEGY=none \
    WORKER_POOL=cpu \
    DJANGO_SECRET_KEY=hf-space-demo-secret-key-change-in-production \
    DJANGO_DEBUG=false \
    DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.hf.space,.huggingface.co \
    BOOTSTRAP_ADMIN_USERNAME=admin \
    BOOTSTRAP_ADMIN_EMAIL=admin@example.local \
    BOOTSTRAP_ADMIN_PASSWORD=admin123 \
    BOOTSTRAP_PROJECT_NAME=Default \
    DEMO_MODE=true \
    DEMO_USERNAME=admin \
    DEMO_PASSWORD=admin123 \
    MINIO_ACCESS_KEY=minioadmin \
    MINIO_SECRET_KEY=minioadmin123 \
    MAX_CONCURRENT_AUDITS=1 \
    MAX_SCENARIOS_PER_RUN=50

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -fsS http://localhost:7860/healthz || exit 1

CMD ["/app/start.sh"]
