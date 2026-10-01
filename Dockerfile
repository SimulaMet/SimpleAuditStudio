# =============================================================================
# SimpleAudit Studio — Minimal Config (Hugging Face Space + local docker run)
#
# Single-process image: Django web + embedded Hatchet + worker in one Python
# process. No Postgres, no supervisord, no external services. SQLite for the
# domain DB, embedded Postgres (sidecar binary) for Hatchet's queue.
#
# HF Spaces only build the root Dockerfile (no compose), so this IS the Space.
#
# Build:  docker build -t simpleaudit-studio .
# Run:    docker run -p 7860:7860 simpleaudit-studio
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
# The core (simpleaudit) is a local path dependency (../SimpleAudit) so the
# studio can use the tracing layer not yet in the published wheel. Clone it
# into the build context before uv sync.
COPY pyproject.toml uv.lock README.md ./
RUN git clone --depth 1 https://github.com/kelkalot/simpleaudit.git /SimpleAudit \
    && pip install uv \
    && uv sync --frozen --no-install-project --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# --- Environment defaults for the Space --------------------------------------
# SIMPLEAUDIT_MINIMAL=1 enables the minimal config path (SQLite + embedded Hatchet).
# Override via HF Space Secrets for anything sensitive.
# Must be set BEFORE collectstatic: Django settings select the DB engine from it,
# and the postgres driver is an optional extra not installed in this image.
ENV SIMPLEAUDIT_MINIMAL=1 \
    PORT=7860 \
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
RUN useradd --create-home appuser \
    && mkdir -p /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://localhost:7860/healthz || exit 1

# Model connections point at OpenAI's real base URL (https://api.openai.com/v1).
# Users add their API key in the UI to run real audits.
CMD ["python", "-m", "simpleaudit_studio.cli"]
