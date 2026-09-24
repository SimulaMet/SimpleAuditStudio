# syntax=docker/dockerfile:1
# =============================================================================
# SimpleAudit Studio — compose-stack application image (web + worker)
#
# This is the image built by docker-compose.yml for the `web`, `worker`, and
# `mock-model` services, and the one CI pushes to GHCR
# (ghcr.io/sushantgautam/simpleauditstudio). It contains only the Django app
# and its dependencies; Postgres and Hatchet run as separate containers in the
# compose stack.
#
# NOTE: This is NOT the root Dockerfile. The root Dockerfile is the
# single-container HF Space image (Postgres + Hatchet + web + worker in one
# container). Do not point docker-compose or a Space at the wrong one.
#
# Build:  docker build -f deploy/compose/Dockerfile -t simpleaudit-studio .
# Run:    see docker-compose.yml
# =============================================================================

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

# curl: healthchecks; git: required to install the SimpleAudit engine from its
# pinned git ref in requirements.txt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Collect static files so Django can serve them without DEBUG.
RUN python manage.py collectstatic --noinput

# Non-root user; media/static dirs must exist before dropping privileges.
RUN useradd --create-home appuser \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# Default entrypoint serves the web API; compose overrides this with
# `python manage.py run_worker` for the worker service.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
