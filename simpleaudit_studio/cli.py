"""CLI entry point for `uvx simpleaudit-studio`.

Boots the full SimpleAudit Studio stack in a single process (minimal config):
  1. Django setup + migrate (SQLite in ~/.simpleaudit-studio; see simpleaudit_studio.paths)
  2. Bootstrap admin user + seed scenario packs + model connections
  3. Start embedded Hatchet engine (sidecar binary + embedded Postgres)
  4. Point model connections at OpenAI's real base URL (or --mock for the built-in mock)
  5. Run Django web server in a daemon thread
  6. Run audit worker in the main thread

Usage:
  uvx simpleaudit-studio              # full stack; models point at OpenAI (add your key in the UI)
  uvx simpleaudit-studio --mock       # use the built-in mock model server (zero-setup demo)
  uvx simpleaudit-studio --port 9000  # custom port

When a needed port is held by another Studio instance or one of its
derivatives (Open WebUI, the Hatchet sidecar), the CLI offers to stop it and
spin cleanly. --no-force-kill declines that offer (and exits with a free-port
suggestion); --yes skips the confirmation.
"""

from __future__ import annotations

import os
import secrets
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
        "--mock", action="store_true",
        help="Use the built-in mock model server (zero-setup demo; results are simulated)",
    )
    parser.add_argument(
        "--disable-chat", "--no-chat", dest="disable_chat", action="store_true",
        help="Do not run the bundled chat (Open WebUI); /chat/ stays unavailable",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not auto-open the web UI in the default browser",
    )
    parser.add_argument(
        "--no-force-kill", action="store_true",
        help="Never stop another Studio instance or its derivatives to free a "
             "port; exit with a free-port suggestion instead",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Answer yes to the force-kill confirmation without asking",
    )
    args = parser.parse_args()

    # Set local mode BEFORE Django reads settings
    os.environ["SIMPLEAUDIT_MINIMAL"] = "1"
    # Chat is part of the bundle; --disable-chat (or SIMPLEAUDIT_CHAT=disabled)
    # opts out.
    if args.disable_chat:
        os.environ["SIMPLEAUDIT_CHAT"] = "off"
    else:
        os.environ.setdefault("SIMPLEAUDIT_CHAT", "embedded")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_SECRET_KEY", "local-insecure-key-change-for-shared-use")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
    os.environ.setdefault("DJANGO_DEBUG", "true")

    import django

    from simpleaudit_studio.paths import adopt_legacy_database, data_dir

    print("=" * 60)
    print("  SimpleAudit Studio — Local Minimal Config")
    print("=" * 60)
    print()
    print(f"💾 Data folder: {data_dir()}  (set SIMPLEAUDIT_DATA_DIR to change)")
    adopted = adopt_legacy_database()
    if adopted:
        source, runs = adopted
        print(f"   Brought over your data from an earlier version ({runs} run{'s' if runs != 1 else ''}):")
        print(f"   {source}")
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
    _ensure_ports_available(args)

    # --- Step 4: Start embedded Hatchet ---
    from infra.minimal_config import start_embedded_hatchet, stop_embedded_hatchet

    start_embedded_hatchet()

    # --- Step 5: Configure model endpoints ---
    # Default: point at OpenAI's real base URL so future runs target OpenAI.
    # --mock: start the built-in mock server and point at it (zero-setup demo).
    mock_server = None
    if args.mock:
        from deploy.mock_openai_server import start_mock_server

        mock_server, mock_port = start_mock_server(port=0)
        mock_url = f"http://127.0.0.1:{mock_port}/v1"
        print(f"🤖 Mock model server at {mock_url}")

        # Point seeded model connections at the live mock server
        _update_model_endpoints(mock_url)
        print("✅ Model endpoints configured (mock).\n")
    else:
        # Repair any connection an earlier version left pointing at the mock,
        # then make sure all connections point at OpenAI's real base URL.
        _restore_real_model_endpoints()
        print("✅ Model endpoints point at OpenAI (add your API key in the UI).\n")

    # --- Step 6: Start Django web server in a daemon thread ---
    port = args.port
    web_thread = threading.Thread(
        target=lambda: call_command("runserver", f"0.0.0.0:{port}", use_reloader=False),
        daemon=True,
    )
    web_thread.start()

    # Give the web server a moment to bind
    time.sleep(1)

    # --- Chat: Open WebUI + its forward-auth proxy ---
    chat_process = None
    from chat import config as chat_config

    if chat_config.ENABLED:
        from chat import proxy as chat_proxy

        print("💬 Starting chat (Open WebUI)...")
        if chat_proxy.is_first_run():
            print("   First start downloads it (~1 GB via uvx) and can take a few minutes.")
            print("   Studio is usable right away; /chat/ works once the download finishes.")
        print(f"   Its data: {chat_proxy.home_dir()}")
        print(f"   Its log:  {chat_proxy.log_path()}")
        try:
            chat_process = start_chat(chat_proxy, port)
        except (OSError, RuntimeError) as exc:
            # No open-webui to run, a taken port, a failed spawn: chat is one
            # part of the stack, so the rest still comes up without it.
            print(f"⚠️  Chat could not start ({exc}); continuing without it.")
            print("   Skip it with --disable-chat.\n")
        else:
            print()

    username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
    password = os.environ.get("BOOTSTRAP_PASSWORD", "admin123")

    # One-time sign-in token: the browser opens the URL below, and the first
    # request that presents it is the only one that works (see auto_login_view).
    auto_login_token = secrets.token_urlsafe(32)
    os.environ["SIMPLEAUDIT_AUTO_LOGIN_TOKEN"] = auto_login_token
    auto_login_url = f"http://localhost:{port}/auto-login/?token={auto_login_token}"
    print("┌─────────────────────────────────────────────────────────┐")
    print("│                                                         │")
    print("│   🚀 SimpleAudit Studio is running!                     │")
    print("│                                                         │")
    print(f"│   Web UI:     http://localhost:{port}                   │")
    print(f"│   Login:      {username} / {password:<20s}│")
    print(f"│   API Docs:   http://localhost:{port}/api/schema/       │")
    if chat_process is not None:
        print(f"│   Chat:       http://localhost:{port}/chat/             │")
    print("│                                                         │")
    if args.mock:
        print("│   Models:     Built-in mock (simulated results)        │")
    else:
        print("│   Models:     OpenAI (add your API key in the UI)       │")
    print("│                                                         │")
    print("│   Press Ctrl+C to stop.                                 │")
    print("└─────────────────────────────────────────────────────────┘")
    print()
    # Single-use sign-in link: opens the browser signed in; if no browser
    # opens, the user can paste this URL manually (it works exactly once).
    print(f"🔑 One-time sign-in link: {auto_login_url}")
    print()

    # Open the default browser signed-in (non-fatal; skip with --no-browser).
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(auto_login_url, port),
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
            if chat_process is not None:
                from chat.proxy import stop_open_webui

                stop_open_webui()
            if mock_server is not None:
                mock_server.shutdown()


def start_chat(chat_proxy, studio_port: int):
    """Start Open WebUI and its proxy, and report readiness in the background.

    Open WebUI takes minutes to be ready on a first run (it is fetched, then it
    migrates its database), so the wait happens in a thread: Studio and the
    worker come up meanwhile, and one line says when /chat/ is live.
    """
    process = chat_proxy.start_open_webui(studio_port)
    chat_proxy.serve(studio_port)

    def report():
        # flush: this lands minutes later, and stdout is block-buffered when the
        # CLI's output is a file or a pipe rather than a terminal.
        if chat_proxy.wait_until_ready(process):
            print(f"\n✅ Chat is ready — http://localhost:{studio_port}/chat/", flush=True)
            print(f"   {_sync_chat_models()}\n", flush=True)
        elif process.poll() is not None:
            print(f"\n⚠️  Chat stopped (exit {process.returncode}). Studio is unaffected.")
            print(f"   What happened: {chat_proxy.log_path()}\n", flush=True)
        else:
            print("\n⚠️  Chat is still not answering. Studio is unaffected.")
            print(f"   What it is doing: {chat_proxy.log_path()}\n", flush=True)

    threading.Thread(target=report, daemon=True).start()
    return process


def _sync_chat_models() -> str:
    """Give the fresh chat Studio's model connections, and say how it went.

    Signals keep it in step afterwards (chat/signals.py); this is the first one,
    for a chat that has just started or was off while connections changed.
    """
    from chat.api import ChatAPIError
    from chat.sync import push_now

    try:
        result = push_now()
    except ChatAPIError as exc:
        return f"Models not synced to chat: {exc}"
    kept = f", kept {result['kept']} added in chat" if result["kept"] else ""
    return f"Synced {result['pushed']} model connection(s) to chat{kept}."


def _ensure_ports_available(args) -> None:
    """Make sure every port the run needs is free, or exit with advice.

    The web port always; with chat, Open WebUI's upstream and the proxy's too.
    A port held by another Studio instance or one of its derivatives can be
    stopped (offered, or automatic with --yes); anything else — or a declined
    offer — ends the run with a free port and the exact flag or environment
    variable that moves the conflicting one.
    """
    from urllib.parse import urlsplit

    from simpleaudit_studio import ports

    force_kill = not args.no_force_kill
    yes = args.yes

    ports.resolve_port_conflict(
        args.port, "the web server", "spin --port <free port>",
        force_kill=force_kill, yes=yes,
    )

    from chat import config as chat_config

    if not chat_config.ENABLED:
        return
    upstream_port = urlsplit(chat_config.UPSTREAM).port or 8080
    ports.resolve_port_conflict(
        upstream_port, "chat's Open WebUI",
        "SIMPLEAUDIT_CHAT_UPSTREAM=http://127.0.0.1:<free port>",
        force_kill=force_kill, yes=yes,
    )
    ports.resolve_port_conflict(
        chat_config.PROXY_PORT, "chat's proxy",
        "SIMPLEAUDIT_CHAT_PROXY_PORT=<free port>",
        force_kill=force_kill, yes=yes,
    )


def _open_browser_when_ready(url: str, port: int, timeout: float = 30.0) -> None:
    """Wait until the web server answers, then open `url` in the default browser.

    The URL is the one-time /auto-login/?token=... link, which signs the
    visitor in and redirects to the dashboard. Readiness is polled on /healthz
    (unauthenticated) so the single-use token is not consumed by the probe.
    Runs in a daemon thread; any failure just prints a hint.
    """
    import requests

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(f"http://localhost:{port}/healthz", timeout=2).status_code == 200:
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


def _restore_real_model_endpoints() -> None:
    """Make sure model connections point at OpenAI's real base URL.

    Seeding already creates the default connection at https://api.openai.com/v1.
    This repairs any connection an earlier version left pointing at the local
    mock server (http://127.0.0.1:PORT/v1) so future runs target OpenAI.
    User-configured custom endpoints (non-local hosts) are left untouched.
    """
    from model_registry.models import ModelConnection

    OPENAI_BASE_URL = "https://api.openai.com/v1"
    for conn in ModelConnection.objects.filter(enabled=True):
        # Extract hostname (strip scheme, path, and port)
        netloc = (conn.base_url or "").split("//", 1)[-1].split("/", 1)[0]
        host = netloc.rsplit(":", 1)[0] if ":" in netloc else netloc
        if host in ("127.0.0.1", "localhost", "0.0.0.0"):
            conn.base_url = OPENAI_BASE_URL
            conn.save(update_fields=["base_url"])


def _run_worker() -> None:
    """Run the Hatchet worker in the main thread (blocks until killed).

    Uses the same start_worker() as the compose deployment, which handles
    startup retries, crash recovery, and the blocking worker loop.
    """
    from infra.worker import start_worker

    start_worker(max_startup_retries=60, startup_retry_delay=2.0)


if __name__ == "__main__":
    main()
