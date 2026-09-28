#!/usr/bin/env python3
"""Django management entrypoint for the production SimpleAudit Studio."""
import os
import sys

# Load `.env` from the repo root if present, so local development works without
# exporting every variable by hand. `override=False` (the default) means real
# environment variables always win — this only fills in what is not already set.
# In Docker Compose the container environment is fully explicit, so this is a
# no-op there.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:  # pragma: no cover - python-dotenv is in pyproject.toml
    pass


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Zero-Docker mode (`dev_server --embedded`) implies SQLite for the domain
    # DB. Decide before Django loads settings: a Postgres `.env` would otherwise
    # make settings import the Postgres driver first.
    if sys.argv[1:2] == ["dev_server"] and "--embedded" in sys.argv:
        os.environ.setdefault("SIMPLEAUDIT_LOCAL_SQLITE", "1")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and available "
            "on your PYTHONPATH environment variable? Did you forget to activate "
            "a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
