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
# git: required to install SimpleAudit engine from its pinned git ref
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
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

# --- Non-root user ------------------------------------------------------------
RUN useradd --create-home appuser \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R appuser:appuser /app
USER appuser

# --- Environment defaults for the Space --------------------------------------
# SIMPLEAUDIT_MINIMAL=1 enables the minimal config path (SQLite + embedded Hatchet).
# Override via HF Space Secrets for anything sensitive.
ENV SIMPLEAUDIT_MINIMAL=1 \
    PORT=7860 \
    DJANGO_SECRET_KEY=hf-space-demo-secret-key-change-in-production \
    DJANGO_DEBUG=false \
    DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.hf.space,.huggingface.co \
    BOOTSTRAP_USERNAME=studio \
    BOOTSTRAP_EMAIL=studio@example.local \
    BOOTSTRAP_PASSWORD=admin123 \
    BOOTSTRAP_PROJECT_NAME=Default \
    DEMO_MODE=true \
    DEMO_USERNAME=studio \
    DEMO_PASSWORD=admin123 \
    MAX_CONCURRENT_AUDITS=1 \
    MAX_SCENARIOS_PER_RUN=50

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://localhost:7860/healthz || exit 1

CMD ["python", "-m", "simpleaudit_studio.cli"]
