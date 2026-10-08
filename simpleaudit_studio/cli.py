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
  uvx simpleaudit-studio --visualize-only --results_dir ./results
                                       # web server only (no worker/hatchet/chat),
                                       # browsing a folder of simpleaudit results

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
        help="Do not run the bundled chat; /playground/ stays unavailable",
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
    parser.add_argument(
        "--visualize-only", action="store_true",
        help="Run only the web server for the result visualizer: skip the audit "
             "worker, embedded Hatchet, chat, and model-endpoint setup. Pair with "
             "--results_dir to browse a folder of simpleaudit results.",
    )
    parser.add_argument(
        "--results_dir", default=None,
        help="Directory of JSON result files for the visualizer's file tree "
             "(replaces `simpleaudit serve`). Point it at a folder where you "
             "dumped simpleaudit results.",
    )
    args = parser.parse_args()

    # Set local mode BEFORE Django reads settings
    os.environ["SIMPLEAUDIT_MINIMAL"] = "1"
    # Pin the explicit mode so `manage.py mode` / resolve_mode() report "embedded".
    os.environ.setdefault("SIMPLEAUDIT_MODE", "embedded")
    # Chat is part of the bundle; --disable-chat (or SIMPLEAUDIT_CHAT=disabled)
    # opts out.
    _set_embedded_chat_defaults(disable_chat=args.disable_chat)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_SECRET_KEY", "local-insecure-key-change-for-shared-use")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
    # Local dev runs with DEBUG off by default so you experience the real
    # production behavior (host checks, static serving, no debug error pages).
    # Set DJANGO_DEBUG=true to get the debug tooling back.
    os.environ.setdefault("DJANGO_DEBUG", "false")

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
    # In visualize-only mode we still need a signed-in user to reach the
    # visualizer pages, so we bootstrap the admin (idempotent) but skip the
    # heavier demo seeding.
    if args.visualize_only:
        print("🌱 Bootstrapping admin user (visualize-only)...")
        _seed_admin_only()
        print("✅ Admin ready.\n")
    else:
        print("🌱 Seeding demo data...")
        _seed_demo_data()
        print("✅ Demo data ready.\n")

    # --- Results dir for the visualizer (replaces `simpleaudit serve`) ---
    if args.results_dir:
        from infra.visualizer import set_results_dir

        resolved = os.path.abspath(os.path.expanduser(args.results_dir))
        if not os.path.isdir(resolved):
            print(f"⚠️  --results_dir '{resolved}' is not a directory; the visualizer file tree will be empty.")
        else:
            set_results_dir(resolved)
            print(f"📂 Visualizer results dir: {resolved}\n")

    # --- Visualize-only: just run the web server, no worker/hatchet/chat ---
    if args.visualize_only:
        _run_visualize_only(args)
        return

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
    from chat import config as chat_config

    # When chat is enabled the front door (Caddy) owns the public port and
    # Studio moves to the internal port; Caddy proxies non-/chat to it.
    web_port = chat_config.INTERNAL_PORT if chat_config.ENABLED else port

    web_thread = threading.Thread(
        target=lambda: call_command("runserver", f"0.0.0.0:{web_port}", use_reloader=False),
        daemon=True,
    )
    web_thread.start()

    # Give the web server a moment to bind
    time.sleep(1)

    # --- Chat: Open WebUI + its forward-auth proxy ---
    chat_process = None

    if chat_config.ENABLED:
        from chat import proxy as chat_proxy

        print("💬 Starting chat (Open WebUI)...")
        if chat_proxy.is_first_run():
            print("   First start installs the subpath wheel and can take a few minutes.")
            print("   Studio is usable right away; /playground/ works once it is ready.")
        print(f"   Its data: {chat_proxy.home_dir()}")
        print(f"   Its log:  {chat_proxy.log_path()}")
        try:
            chat_process = start_chat(chat_proxy, web_port)
        except (OSError, RuntimeError) as exc:
            # No open-webui to run, a taken port, a failed spawn, or no Caddy
            # binary: chat is one part of the stack, so the rest still comes
            # up without it.
            chat_proxy.stop_open_webui()
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
            print(f"│   Playground: http://localhost:{port}/playground/       │")
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


def _set_embedded_chat_defaults(*, disable_chat: bool) -> None:
    """Set embedded chat defaults before Django loads its settings."""
    if disable_chat:
        os.environ["SIMPLEAUDIT_CHAT"] = "off"
        return
    os.environ.setdefault("SIMPLEAUDIT_CHAT", "embedded")
    # Structural spans are useful for future Agent audits. Open WebUI's
    # content-capture controls remain independent and opt-in.
    os.environ.setdefault("SIMPLEAUDIT_CHAT_OTLP", "true")


def start_chat(chat_proxy, internal_port: int):
    """Start Open WebUI and its front door, and report readiness in the background.

    ``internal_port`` is where Studio's web server listens while the front door
    owns the public port (config.PUBLIC_PORT). Open WebUI takes minutes to be
    ready on a first run (its wheel is fetched, then it migrates its database),
    so the wait happens in a thread: Studio and the worker come up meanwhile,
    and one line says when /playground/ is live.
    """
    process = chat_proxy.start_open_webui(internal_port)
    chat_proxy.serve(internal_port)

    from chat import config as _chat_config

    public_port = _chat_config.PUBLIC_PORT

    def report():
        # flush: this lands minutes later, and stdout is block-buffered when the
        # CLI's output is a file or a pipe rather than a terminal.
        if chat_proxy.wait_until_ready(process):
            print(f"\n✅ Playground is ready — http://localhost:{public_port}/playground/", flush=True)
            print(f"   {_sync_chat_models()}\n", flush=True)
            backfilled = _backfill_demo_chat()
            if backfilled:
                print(f"   {backfilled}", flush=True)
            agentic_demo = _backfill_agentic_demo()
            if agentic_demo:
                print(f"   {agentic_demo}", flush=True)
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


def _backfill_demo_chat() -> str:
    """Finish pushing demo KB/tool rows that seeded before the chat existed.

    ``_seed_demo_data`` runs while Open WebUI is down, so its rows are
    local-only (empty ``external_id``) and the OWUI-backed /agents/knowledge/
    and /agents/tools/ pages look empty. Once the chat is ready, this
    completes the push for the bootstrap project. Never raises; reports
    nothing when there is nothing to backfill.
    """
    try:
        from django.contrib.auth import get_user_model

        from accounts.models import Project
        from infra.seed import backfill_demo_chat_resources

        user = get_user_model().objects.order_by("id").first()
        project = Project.objects.order_by("id").first()
        if user is None or project is None:
            return ""
        counts = backfill_demo_chat_resources(project, user)
        if not any(counts.values()):
            return ""
        labels = {"agents": "agent", "knowledge_bases": "knowledge base(s)", "tools": "tool(s)"}
        return "Backfilled demo resources into chat: " + ", ".join(
            f"{counts[k]} {labels[k]}" for k in labels if counts[k]
        )
    except Exception as exc:  # noqa: BLE001 - runs in a startup thread; don't crash it
        print(f"   Demo chat backfill skipped: {exc}", flush=True)
        return ""


def _backfill_agentic_demo() -> str:
    """Preload the synthetic completed Agentic example after Agent sync."""
    from io import StringIO

    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    from accounts.models import Project

    user = get_user_model().objects.order_by("id").first()
    project = Project.objects.order_by("id").first()
    if user is None or project is None:
        return ""
    output = StringIO()
    try:
        call_command("seed_agentic_demo", project=project.id, stdout=output, verbosity=0)
    except Exception as exc:  # noqa: BLE001 - startup demo data must not prevent Studio running
        print(f"   Preloaded Agentic example not ready: {exc}", flush=True)
        return ""
    return output.getvalue().strip()


def _ensure_ports_available(args) -> None:
    """Make sure every port the run needs is free, or exit with advice.

    The public port always (the web server when chat is off, the front door
    when chat is on); with chat, the internal Studio port and Open WebUI's
    upstream port too. A port held by another Studio instance or one of its
    derivatives can be stopped (offered, or automatic with --yes); anything
    else — or a declined offer — ends the run with a free port and the exact
    flag or environment variable that moves the conflicting one.
    """
    from chat import config as chat_config
    from simpleaudit_studio import ports
    ports.ensure_stack_ports(
        args.port,
        chat_enabled=chat_config.ENABLED,
        chat_internal_port=chat_config.INTERNAL_PORT,
        chat_upstream_url=chat_config.UPSTREAM,
        force_kill=not args.no_force_kill,
        yes=args.yes,
        command_hint="spin --port <free port>",
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


def _seed_admin_only() -> None:
    """Bootstrap the admin user + default project only (no demo seeding).

    The visualizer pages sit behind login, so visualize-only mode needs a
    signed-in user; this is the lightweight, idempotent subset of
    ``_seed_demo_data``.
    """
    from accounts.services import bootstrap_admin_and_default_project

    username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
    email = os.environ.get("BOOTSTRAP_EMAIL", "admin@localhost")
    password = os.environ.get("BOOTSTRAP_PASSWORD", "admin123")
    project_name = os.environ.get("BOOTSTRAP_PROJECT_NAME", "Demo Project")

    bootstrap_admin_and_default_project(
        username=username,
        email=email,
        password=password,
        project_name=project_name,
    )


def _run_visualize_only(args) -> None:
    """Run just the Django web server (no worker, Hatchet, or chat).

    This is the client use case: point Studio at a folder of dumped
    ``simpleaudit`` results and browse them in the visualizer. The web server
    runs in the main thread so Ctrl+C stops it cleanly.
    """
    from django.core.management import call_command

    port = args.port

    # Only the web port matters here (no chat/hatchet ports to reserve).
    from simpleaudit_studio import ports

    ports.resolve_port_conflict(
        port, "the web server", "spin --port <free port>",
        force_kill=not args.no_force_kill, yes=args.yes,
    )

    username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
    password = os.environ.get("BOOTSTRAP_PASSWORD", "admin123")

    auto_login_token = secrets.token_urlsafe(32)
    os.environ["SIMPLEAUDIT_AUTO_LOGIN_TOKEN"] = auto_login_token
    # After the one-time sign-in, land on the visualizer instead of the
    # dashboard (which has no useful content in visualize-only mode).
    os.environ["SIMPLEAUDIT_AUTO_LOGIN_NEXT"] = "/visualizer/"
    auto_login_url = f"http://localhost:{port}/auto-login/?token={auto_login_token}"

    print("┌─────────────────────────────────────────────────────────┐")
    print("│   👁️  SimpleAudit Studio — Visualize-only mode          │")
    print("└─────────────────────────────────────────────────────────┘")
    print()
    print(f"   Web UI:        http://localhost:{port}")
    print(f"   Visualizer:    http://localhost:{port}/visualizer/")
    print(f"   Drag-drop:     http://localhost:{port}/visualizer/upload/")
    print(f"   Login:         {username} / {password}")
    if args.results_dir:
        print(f"   Results dir:   {os.path.abspath(os.path.expanduser(args.results_dir))}")
    print()
    print(f"🔑 One-time sign-in link: {auto_login_url}")
    print()
    print("   Press Ctrl+C to stop.")
    print()

    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready,
            args=(auto_login_url, port),
            daemon=True,
        ).start()

    try:
        call_command("runserver", f"0.0.0.0:{port}", use_reloader=False)
    except KeyboardInterrupt:
        print("\n👋 Shutting down.")


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

    # Seed the regular demo resources and both scenario packs before Open WebUI
    # starts; the precomputed Agentic run waits until the Agent is synced.
    call_command("seed_platform", project=project.id, verbosity=0)
    call_command("seed_agentic_scenarios", project=project.id, verbosity=0)


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
