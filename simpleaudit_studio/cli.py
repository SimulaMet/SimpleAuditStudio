"""CLI entry point for `uvx simpleaudit-studio`.

Boots the full SimpleAudit Studio stack in a single process (minimal config):
  1. Django setup + migrate (SQLite)
  2. Bootstrap admin user + seed scenario packs + model connections
  3. Start embedded Hatchet engine (sidecar binary + embedded Postgres)
  4. Start mock OpenAI server (unless --no-mock)
  5. Run Django web server in a daemon thread
  6. Run audit worker in the main thread

Usage:
  uvx simpleaudit-studio              # full stack with mock models
  uvx simpleaudit-studio --no-mock    # skip mock server (use your own endpoints)
  uvx simpleaudit-studio --port 9000  # custom port
"""

from __future__ import annotations

import os
import signal
import threading
import time
import webbrowser


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="simpleaudit-studio",
        description="Run SimpleAudit Studio locally (minimal config, no Docker).",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8000")),
        help="Web server port (default: 8000)",
    )
    parser.add_argument(
        "--no-mock", action="store_true",
        help="Skip the built-in mock model server (use your own endpoints)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not auto-open the web UI in the default browser",
    )
    args = parser.parse_args()

    # Set local mode BEFORE Django reads settings
    os.environ["SIMPLEAUDIT_MINIMAL"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_SECRET_KEY", "local-insecure-key-change-for-shared-use")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
    os.environ.setdefault("DJANGO_DEBUG", "true")

    import django

    print("=" * 60)
    print("  SimpleAudit Studio — Local Minimal Config")
    print("=" * 60)
    print()

    # --- Step 1: Django setup + migrate ---
    print("📦 Setting up Django...")
    django.setup()

    from django.core.management import call_command

    print("🗄️  Running migrations (SQLite)...")
    call_command("migrate", verbosity=0, interactive=False)
    print("✅ Migrations complete.")

    # --- Step 2: Bootstrap admin + seed data ---
    print("🌱 Seeding demo data...")
    _seed_demo_data()
    print("✅ Demo data ready.\n")

    # --- Step 3: Pre-check port availability ---
    _check_port_available(args.port)

    # --- Step 4: Start embedded Hatchet ---
    from infra.minimal_config import start_embedded_hatchet, stop_embedded_hatchet

    start_embedded_hatchet()

    # --- Step 5: Start mock OpenAI server (unless --no-mock) ---
    mock_server = None
    if not args.no_mock:
        from deploy.mock_openai_server import start_mock_server

        mock_server, mock_port = start_mock_server(port=0)
        mock_url = f"http://127.0.0.1:{mock_port}/v1"
        print(f"🤖 Mock model server at {mock_url}")

        # Point seeded model connections at the live mock server
        _update_model_endpoints(mock_url)
        print("✅ Model endpoints configured.\n")
    else:
        print("⏭️  Skipping mock server (--no-mock). Configure your own model endpoints in the UI.\n")

    # --- Step 6: Start Django web server in a daemon thread ---
    port = args.port
    web_thread = threading.Thread(
        target=lambda: call_command("runserver", f"0.0.0.0:{port}", use_reloader=False),
        daemon=True,
    )
    web_thread.start()

    # Give the web server a moment to bind
    time.sleep(1)

    username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
    password = os.environ.get("BOOTSTRAP_PASSWORD", "admin123")

    auto_login_url = f"http://localhost:{port}/auto-login/"
    print("┌─────────────────────────────────────────────────────────┐")
    print("│                                                         │")
    print("│   🚀 SimpleAudit Studio is running!                     │")
    print("│                                                         │")
    print(f"│   Web UI:     http://localhost:{port}                   │")
    print(f"│   Login:      {username} / {password:<20s}│")
    print(f"│   API Docs:   http://localhost:{port}/api/schema/       │")
    print("│                                                         │")
    if not args.no_mock:
        print("│   Models:     Built-in mock (swap for real in UI)      │")
    else:
        print("│   Models:     Configure your own endpoints in the UI    │")
    print("│                                                         │")
    print("│   Press Ctrl+C to stop.                                 │")
    print("└─────────────────────────────────────────────────────────┘")
    print()

    # Open the default browser signed-in (non-fatal; skip with --no-browser).
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(auto_login_url,),
            daemon=True,
        ).start()

    # --- Step 7: Run worker in the MAIN thread (required for signal handlers) ---
    print("🔧 Starting audit worker (main thread)...")

    # Handle SIGTERM (kill <pid>) the same way as Ctrl+C so the embedded
    # Postgres + Hatchet sidecar are stopped cleanly instead of orphaned.
    def _sigterm_handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        _run_worker()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n👋 Shutting down...")
        try:
            stop_embedded_hatchet()
        finally:
            if mock_server is not None:
                mock_server.shutdown()


def _check_port_available(port: int) -> None:
    """Exit early with a clear message if the web port is already in use."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            print(f"\n✗ Port {port} is already in use.")
            print("  Is another SimpleAudit Studio instance running?")
            print(f"  Try a different port: spin --port {port + 1}\n")
            raise SystemExit(1)


def _open_browser_when_ready(url: str, timeout: float = 30.0) -> None:
    """Wait until the web server answers, then open `url` in the default browser.

    The URL is /auto-login/, which signs the visitor in and redirects to the
    dashboard. Runs in a daemon thread; any failure just prints a hint.
    """
    import requests

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # allow_redirects=False: we only need proof the endpoint exists.
            if requests.get(url, timeout=2).status_code in (200, 302):
                break
        except requests.RequestException:
            time.sleep(0.5)
    else:
        print(f"⚠️  Web UI did not come up within {timeout:.0f}s — open {url} manually.")
        return
    if not webbrowser.open(url):
        print(f"⚠️  Could not open a browser automatically — visit {url} manually.")


def _seed_demo_data() -> None:
    """Bootstrap admin user, default project, scenario packs, and model connections."""
    from django.core.management import call_command

    from accounts.services import bootstrap_admin_and_default_project

    # Respect env vars (set by Dockerfile for HF Space, or defaults for local)
    username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
    email = os.environ.get("BOOTSTRAP_EMAIL", "admin@localhost")
    password = os.environ.get("BOOTSTRAP_PASSWORD", "admin123")
    project_name = os.environ.get("BOOTSTRAP_PROJECT_NAME", "Demo Project")

    _user, project = bootstrap_admin_and_default_project(
        username=username,
        email=email,
        password=password,
        project_name=project_name,
    )

    # Seed scenario packs + model connections + demo audit runs (all idempotent)
    call_command("seed_platform", project=project.id, verbosity=0)


def _update_model_endpoints(mock_url: str) -> None:
    """Point all seeded model connections at the local mock server.

    The mock server ignores auth entirely, so no API key is set — the
    connection stays keyless (shown as "⚠ no key" in the UI).
    """
    from model_registry.models import ModelConnection

    ModelConnection.objects.filter(enabled=True).update(
        base_url=mock_url,
        api_key_direct="",
        secret_reference="",
    )


def _run_worker() -> None:
    """Run the Hatchet worker in the main thread (blocks until killed).

    Uses the same start_worker() as the compose deployment, which handles
    startup retries, crash recovery, and the blocking worker loop.
    """
    from infra.worker import start_worker

    start_worker(max_startup_retries=60, startup_retry_delay=2.0)


if __name__ == "__main__":
    main()
