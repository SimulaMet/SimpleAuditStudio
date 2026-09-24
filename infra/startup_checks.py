"""Startup validation for production configuration."""
import os


def _local_sqlite_mode() -> bool:
    return (
        os.environ.get("SIMPLEAUDIT_LOCAL_SQLITE", "").strip().lower() in {"1", "true", "yes", "on"}
        or os.environ.get("SIMPLEAUDIT_MINIMAL", "").strip() == "1"
    )


def validate_startup_environment(bootstrap_password: str | None = None) -> list[str]:
    """Return blocking configuration errors.

    This is intentionally conservative: it blocks only unsafe or obviously
    misconfigured production values. Warnings are returned separately by the
    caller if needed.

    In the opt-in local SQLite test mode (SIMPLEAUDIT_LOCAL_SQLITE) the
    Postgres/MinIO infrastructure checks are skipped because those services do
    not exist locally; the canonical deployment still enforces them.
    """
    errors = []
    local_sqlite = _local_sqlite_mode()
    debug = os.environ.get("DJANGO_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
    allowed_hosts = {host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if host.strip()}
    public_host_indicators = {"*", "0.0.0.0"}

    if not debug and allowed_hosts & public_host_indicators:
        errors.append("DJANGO_ALLOWED_HOSTS contains public wildcard hosts while DJANGO_DEBUG=false.")

    if not local_sqlite:
        secret_key = os.environ.get("DJANGO_SECRET_KEY", "")
        if secret_key in {"", "change-me"}:
            errors.append("DJANGO_SECRET_KEY must be set to a strong unique value.")

    if not local_sqlite:
        postgres_password = os.environ.get("POSTGRES_PASSWORD", "")
        if postgres_password in {"", "change-me"}:
            errors.append("POSTGRES_PASSWORD must be set and must not be change-me.")

        # MinIO is optional — only validate keys if the endpoint is configured
        minio_endpoint = os.environ.get("MINIO_ENDPOINT", "")
        if minio_endpoint:
            minio_access_key = os.environ.get("MINIO_ACCESS_KEY", "")
            minio_secret_key = os.environ.get("MINIO_SECRET_KEY", "")
            if minio_access_key in {"", "change-me"} or minio_secret_key in {"", "change-me"}:
                errors.append("MINIO_ACCESS_KEY and MINIO_SECRET_KEY must be set when MINIO_ENDPOINT is configured.")

    effective_bootstrap_password = bootstrap_password if bootstrap_password is not None else os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if effective_bootstrap_password in {"", "change-me"}:
        errors.append("BOOTSTRAP_ADMIN_PASSWORD must be set and must not be change-me.")

    return errors
