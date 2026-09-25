# syntax=docker/dockerfile:1
# =============================================================================
# SimpleAudit Studio — compose-stack application image (web + worker)
#
# This is the image built by docker-compose.yml for the `web`, `worker`, and
# `mock-model` services. It contains only the Django app and its dependencies;
# Postgres and Hatchet run as separate containers in the compose stack.
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

# curl: healthchecks
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# pyproject.toml is the single source of truth; uv.lock pins exact versions.
# The compose stack runs against external Postgres (postgres extra) and serves
# via gunicorn (server extra). uv sync creates /app/.venv; the PATH update
# keeps the `python`/`gunicorn` entrypoints working.
COPY pyproject.toml uv.lock README.md ./
RUN pip install uv \
    && uv sync --frozen --no-install-project --no-dev --extra postgres --extra server
ENV PATH="/app/.venv/bin:$PATH"

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
