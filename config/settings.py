"""Django settings for SimpleAudit Studio.

Production runs on PostgreSQL with Hatchet. SQLite is used only for local
development (SIMPLEAUDIT_LOCAL_SQLITE) and the single-container demo
(see infra/minimal_config.py).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _pkg_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("simpleaudit-studio")
    except PackageNotFoundError:
        return "dev"


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "*")


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
    # When ALLOWED_HOSTS is a bare wildcard (*), skip derivation — the fallbacks
    # below already cover HF Spaces and local dev origins.
    if "*" not in ALLOWED_HOSTS:
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
    # Hardcoded fallbacks for known deployment targets. These guarantee CSRF
    # works on HF Spaces even if the platform injects/overrides DJANGO_ALLOWED_HOSTS
    # before our derivation runs. Local dev origins are always included.
    fallbacks = [
        "http://localhost",
        "http://127.0.0.1",
        "https://*.hf.space",
        "https://*.huggingface.co",
    ]
    # In demo mode the app is embedded in an iframe on huggingface.co (the Space
    # page). Form POSTs from inside that frame carry Origin: https://huggingface.co,
    # so it must be a trusted origin or Django rejects them with 403 "CSRF
    # verification failed". The wildcard above does NOT match the bare domain.
    if env_bool("DEMO_MODE", False):
        fallbacks.append("https://huggingface.co")
    combined = list(explicit)
    for origin in derived + fallbacks:
        if origin not in combined:
            combined.append(origin)
    return combined


CSRF_TRUSTED_ORIGINS = _csrf_trusted_origins()

# The app runs behind a TLS-terminating reverse proxy (HF Spaces, nginx, etc.).
# Without this, Django sees the proxied request as plain HTTP and
# build_absolute_uri() emits http:// URLs — which makes og:url/og:image/canonical
# point at the http:// origin (Facebook's debugger then follows the 301 to
# https://host:443/ and warns about the mismatch). Trusting the proxy header
# makes absolute URLs use https:// like the browser actually sees.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    # SimpleAudit Studio apps
    "accounts",
    "scenarios",
    "model_registry",
    "judges",
    "audits",
    "infra",
    # Optional module: its URLs 404 and nothing runs unless SIMPLEAUDIT_CHAT is
    # set (chat/config.py). Installed either way so its templates, management
    # commands and tests resolve.
    "chat",
]

MIDDLEWARE = [
    "infra.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "infra.middleware.TokenSessionBridgeMiddleware",
    "infra.middleware.CsrfCookieMiddleware",
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
                "infra.context_processors.gravatar_url",
                "infra.context_processors.workspaces",
                "infra.context_processors.write_access",
                "infra.context_processors.nav",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

AUTH_USER_MODEL = "accounts.User"

# Local demo mode: one-liner run via `uvx simpleaudit-studio`.
# Uses SQLite + embedded Hatchet + mock model server. Never for production.
# Distinct from DEMO_MODE (HF Spaces) — this is purely local dev convenience.
MINIMAL_CONFIG = os.environ.get("SIMPLEAUDIT_MINIMAL", "").strip() == "1"

# SQLite is shared by the web server and worker threads: WAL lets reads run
# during writes, IMMEDIATE takes the write lock up front (no upgrade deadlocks),
# and the timeout waits for a busy lock instead of failing.
SQLITE_OPTIONS = {
    "timeout": 30,
    "transaction_mode": "IMMEDIATE",
    "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
}

# Canonical runtime is PostgreSQL. The SQLite branches below are
# explicit, opt-in LOCAL-ONLY conveniences. They are never the default and
# must not be used in any deployment.
if MINIMAL_CONFIG:
    # Kept outside the installed package (simpleaudit_studio.paths), so an
    # upgrade or `uv cache clean` doesn't start from an empty database.
    from simpleaudit_studio.paths import database_path

    database_path().parent.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": database_path(),
            "OPTIONS": SQLITE_OPTIONS,
        }
    }
elif env_bool("SIMPLEAUDIT_LOCAL_SQLITE", False):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "local_test.sqlite3",
            "OPTIONS": SQLITE_OPTIONS,
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
            # Reuse connections for 60 s, but check them first, so a database restart
            # or dropped idle connection costs one reconnect, not a failed request.
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "connect_timeout": 10,
                "application_name": "simpleaudit-studio",
                # Detect dead peers (e.g. after a network blip) instead of hanging.
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
            },
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
STATICFILES_DIRS = [BASE_DIR / "static"]

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
    "VERSION": _pkg_version(),
    "SERVE_INCLUDE_SCHEMA": False,
}

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# WorkOS Magic Auth (passwordless email codes). Enabled only when both values are
# configured; the login page hides the option otherwise.
WORKOS_CLIENT_ID = os.environ.get("WORKOS_CLIENT_ID", "")
WORKOS_API_KEY = os.environ.get("WORKOS_API_KEY", "")
WORKOS_ENABLED = bool(WORKOS_CLIENT_ID and WORKOS_API_KEY)

# Demo mode: prefill the login form with demo credentials and show a hint banner
# (public demos / HF Spaces).
DEMO_MODE = env_bool("DEMO_MODE", False)
DEMO_USERNAME = os.environ.get("DEMO_USERNAME", "studio")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "admin123")

# Framing policy:
# - Demo mode (HF Spaces / public demos): allow cross-origin framing so the
#   Space page on huggingface.co can embed the app served from *.hf.space.
# - Normal deployments: SAMEORIGIN keeps clickjacking protection intact.
X_FRAME_OPTIONS = "ALLOWALL" if DEMO_MODE else "SAMEORIGIN"

# Cross-site cookie policy for demo mode. The app is embedded in an iframe on
# huggingface.co, which makes every form POST a *cross-site* request from the
# browser's perspective. Cookies with SameSite=Lax (Django default) are not
# sent on cross-site POSTs, so the CSRF token never reaches the server and
# login fails with 403. In demo mode we relax to SameSite=None + Secure so the
# csrftoken/sessionid cookies flow inside the embed. This is safe here because
# *.hf.space is always HTTPS; normal deployments keep the strict Lax default.
if DEMO_MODE:
    SESSION_COOKIE_SAMESITE = "None"
    CSRF_COOKIE_SAMESITE = "None"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Production security headers (per Django's production checklist). The app runs
# behind a TLS-terminating proxy (see SECURE_PROXY_SSL_HEADER above), so these
# are safe to enable. They are gated off for local dev (http://localhost), the
# local minimal/demo bundle, DEMO_MODE (which sets its own cookie policy), and
# the test suite (which runs over http://localhost with DEBUG=false) so they
# never break a non-HTTPS or cross-site-embedded setup.
_TESTING = (
    SECRET_KEY in {"test-secret-key-not-change-me", "ci-secret-key"}
    or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    or env_bool("SIMPLEAUDIT_TESTING", False)
)
if not (DEBUG or MINIMAL_CONFIG or DEMO_MODE or _TESTING):
    # TLS termination and the HTTP→HTTPS redirect are owned by the reverse
    # proxy (Cloudflare / nginx / Caddy).  Django trusts the proxy via
    # SECURE_PROXY_SSL_HEADER (set above) and does NOT issue its own redirect —
    # doing so would break local dev and any deployment where the proxy already
    # handles the redirect.
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# Operational settings used by health checks and bootstrap commands.
if MINIMAL_CONFIG:
    # In local demo mode the embedded Hatchet client provides its own connection
    # details at runtime; these placeholders are never used for real connections.
    HATCHET_SERVER_URL = "http://127.0.0.1:28243"
    HATCHET_GRPC_URL = "127.0.0.1:7070"
    HATCHET_API_KEY = ""
    HATCHET_TLS_STRATEGY = "none"
else:
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

# Visualizer: results directory for JSON files (set via env var or CLI)
VISUALIZER_RESULTS_DIR = os.environ.get("VISUALIZER_RESULTS_DIR")
