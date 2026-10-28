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
    command = sys.argv[1] if len(sys.argv) > 1 else None
    argv = sys.argv
    # Single-process (no-Docker) runs: `dev`, `dev_server --embedded`, or an
    # explicit SIMPLEAUDIT_MODE in {dev, embedded} (the env spelling must
    # behave exactly like the flag). config.runtime is dependency-free.
    from config import runtime

    if command == "dev":
        os.environ.setdefault("SIMPLEAUDIT_MODE", "dev")
        # Handle lifecycle requests before django.setup(), migrations, or any
        # imports that lock in chat ports/settings.
        from pathlib import Path

        from simpleaudit_studio.lifecycle import ACTIONS, Instance, act, start

        instance = Instance("dev", Path(__file__).resolve().parent)
        rest = sys.argv[2:]
        action = rest[0] if rest and rest[0] in ACTIONS else None
        if act(instance, action, yes="--yes" in rest):
            return
        if action in {"reset", "restart"}:
            sys.argv = [sys.argv[0], "dev"] + [x for x in rest[1:] if x != "--yes"]
            rest = sys.argv[2:]
        requested = int(os.environ.get("PORT", "8000"))
        explicit = False
        for index, arg in enumerate(rest):
            if arg == "--port" and index + 1 < len(rest):
                requested = int(rest[index + 1])
                explicit = True
            elif arg.startswith("--port="):
                requested = int(arg.split("=", 1)[1])
                explicit = True
        if not start(instance, requested_port=requested, explicit_port=explicit):
            return
    single_process = runtime.is_single_process_run(command, sys.argv)
    # Zero-Docker modes imply SQLite for the domain DB. Decide before Django
    # loads settings: a Postgres `.env` would otherwise make settings import
    # the Postgres driver first.
    if single_process:
        os.environ.setdefault("SIMPLEAUDIT_LOCAL_SQLITE", "1")
        # Chat (Open WebUI) is on by default in the single-process modes and
        # must always be disableable — by the --disable-chat/--no-chat flag
        # (which wins) or SIMPLEAUDIT_CHAT=off in the environment.
        # Precedence: flag > env > default. This must happen BEFORE
        # django.setup(), because chat.config.ENABLED is frozen the moment
        # the chat app is imported.
        if runtime.has_chat_disable_flag(argv):
            os.environ["SIMPLEAUDIT_CHAT"] = "off"  # explicit flag beats .env
        else:
            os.environ.setdefault("SIMPLEAUDIT_CHAT", "embedded")
            # OTLP tracing for OWUI spans must also be set before django.setup()
            # because chat/config.py freezes OTLP_ENABLED at import time.
            os.environ.setdefault("SIMPLEAUDIT_CHAT_OTLP", "true")
    # `dev` / `dev_server` are local dev commands — they imply DEBUG so the
    # production hardening block (SECURE_SSL_REDIRECT, HSTS, secure cookies)
    # stays off.
    if command in {"dev", "dev_server"}:
        os.environ.setdefault("DJANGO_DEBUG", "1")
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
