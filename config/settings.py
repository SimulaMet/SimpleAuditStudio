"""Django settings for the production SimpleAudit Studio.

The canonical runtime is Docker Compose with PostgreSQL, MinIO, and a durable
workflow system. SQLite is intentionally not configured here.
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")


def _csrf_trusted_origins() -> list[str]:
    """Build CSRF_TRUSTED_ORIGINS from explicit env + derived from ALLOWED_HOSTS.

    Django's CSRF protection requires the request Origin to match a trusted
    origin (scheme + host, no trailing slash). ALLOWED_HOSTS alone does NOT
    satisfy this — without CSRF_TRUSTED_ORIGINS, every POST from the browser
    (login, register, forms) is rejected with 403 "Origin checking failed".

    We accept an explicit DJANGO_CSRF_TRUSTED_ORIGINS (comma-separated full
    URLs) and additionally derive https://<host> for each ALLOWED_HOSTS entry
    that looks like a real domain (so self-hosted / HF Space deploys work
    turnkey). Wildcard subdomains (e.g. .hf.space) are expanded to the scheme
    form Django expects (https://*.hf.space).
    """
    explicit = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
    derived: list[str] = []
    for host in ALLOWED_HOSTS:
        if not host:
            continue
        if host.startswith("."):
            # Wildcard subdomain -> https://*.example.com (Django's expected form)
            derived.append(f"https://*{host}")
        elif ":" in host:
            # Host with an explicit port (e.g. localhost:8000) — use http for
            # loopback, https otherwise. Strip nothing; Django accepts the port.
            scheme = "http" if host.split(":")[0] in {"localhost", "127.0.0.1", "0.0.0.0"} else "https"
            derived.append(f"{scheme}://{host}")
        elif host in {"localhost", "127.0.0.1", "0.0.0.0"}:
            derived.append(f"http://{host}")
        else:
            derived.append(f"https://{host}")
    combined = list(explicit)
    for origin in derived:
        if origin not in combined:
            combined.append(origin)
    return combined


CSRF_TRUSTED_ORIGINS = _csrf_trusted_origins()

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    # SimpleAudit Studio apps
    "accounts",
    "scenarios",
    "model_registry",
    "audits",
    "infra",
]

MIDDLEWARE = [
    "infra.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "infra.middleware.ProjectMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "infra.context_processors.admin_status",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

AUTH_USER_MODEL = "accounts.User"

# Canonical runtime is PostgreSQL (ADR 002). The SQLite branch below is an
# explicit, opt-in LOCAL-ONLY convenience for running the test suite on a
# machine without Postgres/Docker. It is never the default and must not be used
# in any deployment.
if env_bool("SIMPLEAUDIT_LOCAL_SQLITE", False):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "local_test.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "simpleaudit"),
            "USER": os.environ.get("POSTGRES_USER", "simpleaudit"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "postgres"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Serve the SPA's static assets (app.js/app.css) from the repo in development.
# In production, `collectstatic` copies them to STATIC_ROOT and a reverse proxy
# or WhiteNoise serves them.
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "infra.exceptions.api_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SimpleAudit Studio API",
    "DESCRIPTION": "Production API for reproducible AI audits.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Operational settings used by health checks and bootstrap commands.
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "simpleaudit-artifacts")
HATCHET_SERVER_URL = os.environ.get("HATCHET_SERVER_URL", "http://hatchet-server:8888")
HATCHET_GRPC_URL = os.environ.get("HATCHET_GRPC_URL", "hatchet-server:7077")
HATCHET_API_KEY = os.environ.get("HATCHET_API_KEY", "")
# gRPC transport security for the worker/client. The compose deployment runs a
# plaintext gRPC endpoint (SERVER_GRPC_INSECURE=t), so the default is "none".
# Set to "tls" or "mtls" (with HATCHET_CLIENT_TLS_* env vars) for TLS deployments.
HATCHET_TLS_STRATEGY = os.environ.get("HATCHET_TLS_STRATEGY", "none")
WORKER_POOL = os.environ.get("WORKER_POOL", "cpu")
# NOTE: SimpleAudit engine provenance (version + optional commit) is NOT a
# setting here. It is resolved from the installed package metadata at runtime by
# infra.simpleaudit_package.resolve_engine_provenance(), so the web and worker
# always agree on what engine they actually have. See that module for details.
MAX_CONCURRENT_AUDITS = int(os.environ.get("MAX_CONCURRENT_AUDITS", "2"))
MAX_SCENARIOS_PER_RUN = int(os.environ.get("MAX_SCENARIOS_PER_RUN", "500"))
SSE_MAX_CONNECTIONS_PER_USER = int(os.environ.get("SSE_MAX_CONNECTIONS_PER_USER", "10"))

# --- Sentry error tracking & tracing ---
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        send_default_pii=True,
        enable_logs=True,
        traces_sample_rate=1.0 if DEBUG else 0.1,
        profile_session_sample_rate=1.0 if DEBUG else 0.1,
        profile_lifecycle="trace",
        environment=os.environ.get("SENTRY_ENVIRONMENT", "development" if DEBUG else "production"),
    )

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation": {
            "()": "infra.middleware.CorrelationLogFilter",
        },
    },
    "formatters": {
        "json": {
            "()": "infra.logging.JsonFormatter",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["correlation"],
        }
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("LOG_LEVEL", "INFO"),
    },
}
