# =============================================================================
# SimpleAudit Studio — single-container image (local `docker run` + HF Space)
#
# Single-process image: Django web + embedded Hatchet + worker in one Python
# process. No Postgres, no supervisord, no external services. SQLite for the
# domain DB, embedded Postgres (sidecar binary) for Hatchet's queue.
#
# The container listens on 7860 (Hugging Face Spaces expect that port).
#
# Build:  docker build -t simpleaudit-studio .
# Run:    docker run -d -p 8000:7860 -v sa-data:/data --name simpleaudit-studio simpleaudit-studio
#         (web → http://localhost:8000, login studio / admin123 — override with
#          -e BOOTSTRAP_PASSWORD=<strong> for anything shared)
# All data (SQLite database, embedded queue, chat) lives in /data: mount a
# named volume there to keep it across restarts and image updates.
# =============================================================================

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

# --- System dependencies -----------------------------------------------------
# curl: healthchecks
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# --- Python dependencies -----------------------------------------------------
# pyproject.toml is the single source of truth; uv.lock pins exact versions.
# uv sync creates /app/.venv; the PATH update keeps the `python` entrypoint.
# The core (simpleaudit) is a PyPI dependency; uv sync pulls it from the
# registry (no local checkout needed).
COPY pyproject.toml uv.lock README.md ./
RUN pip install uv \
    && uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# --- Environment defaults -----------------------------------------------------
# SIMPLEAUDIT_MINIMAL=1 enables the single-process path (SQLite + embedded
# Hatchet); SIMPLEAUDIT_DATA_DIR points everything (database, embedded queue,
# chat) at /data so a mounted volume persists it. PORT=7860 is what Hugging
# Face Spaces expect. Override via `docker run -e ...` or HF Space environment
# variables for anything sensitive.
# Must be set BEFORE collectstatic: Django settings select the DB engine from
# it, and the postgres driver is an optional extra not installed in this image.
ENV SIMPLEAUDIT_MINIMAL=1 \
    PORT=7860 \
    SIMPLEAUDIT_DATA_DIR=/data \
    DJANGO_SECRET_KEY=hf-space-demo-secret-key-change-in-production \
    DJANGO_DEBUG=false \
    DJANGO_ALLOWED_HOSTS=* \
    BOOTSTRAP_USERNAME=studio \
    BOOTSTRAP_EMAIL=studio@example.local \
    BOOTSTRAP_PASSWORD=admin123 \
    BOOTSTRAP_PROJECT_NAME=Default \
    DEMO_MODE=true \
    DEMO_USERNAME=studio \
    DEMO_PASSWORD=admin123

# --- Application code --------------------------------------------------------
COPY . .

# Collect static files so Django can serve them without DEBUG.
RUN python manage.py collectstatic --noinput

# --- Non-root user ------------------------------------------------------------
# /data is where a mounted named volume persists all app data; it must exist
# and be writable by appuser before the user switch.
RUN useradd --create-home appuser \
    && mkdir -p /app/staticfiles /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://localhost:7860/healthz || exit 1

# Model connections point at OpenAI's real base URL (https://api.openai.com/v1).
# Users add their API key in the UI to run real audits.
CMD ["python", "-m", "simpleaudit_studio.cli"]
