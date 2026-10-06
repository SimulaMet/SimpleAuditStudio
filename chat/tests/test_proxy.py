"""The forward-auth proxy, against a stub Studio and a stub Open WebUI.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test chat.tests.test_proxy
"""
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from chat import config, proxy

VALID_COOKIE = "sessionid=good"


class _StubOpenWebUI(BaseHTTPRequestHandler):
    """Echoes the identity it was given, and sets a session token cookie."""

    protocol_version = "HTTP/1.1"
    last_path = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).last_path = self.path
        body = json.dumps({
            "email": self.headers.get(config.EMAIL_HEADER),
            "role": self.headers.get(config.ROLE_HEADER),
            "cookie_seen": self.headers.get("Cookie"),
            # A duplicate is exactly what a casing mismatch produces, and a
            # plain lookup would never see it.
            "identity_headers_seen": sum(
                1 for name in self.headers
                if name.lower() == config.EMAIL_HEADER.lower()
            ),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "token=someones-jwt; Path=/")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ReadinessTests(SimpleTestCase):
    """wait_until_ready must not be fooled by *another* Open WebUI on the port.

    The health URL is fixed, so a 200 can come from an instance that was
    already there when ours died on the bind.
    """

    def setUp(self):
        self.upstream = _serve(_StubOpenWebUI)
        self.patcher = patch.object(config, "UPSTREAM",
                                    f"http://127.0.0.1:{self.upstream.server_address[1]}")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.upstream.shutdown()
        super().tearDown()

    def test_a_200_from_a_dead_process_is_not_readiness(self):
        process = MagicMock()
        process.poll.return_value = 0   # ours died on the bind
        self.assertFalse(proxy.wait_until_ready(process, timeout=5))

    def test_a_200_from_a_live_process_is_readiness(self):
        process = MagicMock()
        process.poll.return_value = None
        self.assertTrue(proxy.wait_until_ready(process, timeout=5))


class PidFileTests(SimpleTestCase):
    """The pid file must never trample or orphan another run's Open WebUI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.file_patcher = patch.object(proxy, "pid_file", lambda: self.tmp / "open-webui.pid")
        self.file_patcher.start()
        # These tests only exercise pid-file bookkeeping, not the launch.
        self.interp_patcher = patch.object(
            proxy, "_locate_owui_interpreter", lambda home: "/usr/bin/env python")
        self.interp_patcher.start()
        self.secret_patcher = patch.object(proxy, "_secret_key", lambda home: "test-key")
        self.secret_patcher.start()
        self._process = proxy._process
        proxy._process = None

    def tearDown(self):
        proxy._process = self._process
        self.interp_patcher.stop()
        self.secret_patcher.stop()
        self.file_patcher.stop()
        self._tmp.cleanup()
        super().tearDown()

    def test_stop_does_not_delete_a_file_recording_someone_elses_pid(self):
        file = self.tmp / "open-webui.pid"
        file.write_text("424242")
        process = MagicMock()
        process.pid = 1234
        process.poll.return_value = 0   # already dead: nothing to terminate
        proxy._process = process
        proxy.stop_open_webui()
        self.assertEqual(file.read_text().strip(), "424242")

    def test_stop_deletes_a_file_recording_our_pid(self):
        file = self.tmp / "open-webui.pid"
        process = MagicMock()
        process.pid = 1234
        process.poll.return_value = 0
        proxy._process = process
        file.write_text("1234")
        proxy.stop_open_webui()
        self.assertFalse(file.exists())

    def test_spawn_does_not_overwrite_a_file_recording_a_live_pid(self):
        file = self.tmp / "open-webui.pid"
        file.write_text("424242")
        fake = MagicMock()
        fake.pid = 1234
        with patch.object(proxy.subprocess, "Popen", return_value=fake), \
             patch.object(proxy, "log_path", lambda: self.tmp / "server.log"), \
             patch("infra.minimal_config._pid_alive", return_value=True):
            proxy._spawn(self.tmp)
        self.assertEqual(file.read_text().strip(), "424242")
        proxy._process = None

    def test_spawn_overwrites_a_file_recording_a_dead_pid(self):
        file = self.tmp / "open-webui.pid"
        file.write_text("424242")
        fake = MagicMock()
        fake.pid = 1234
        with patch.object(proxy.subprocess, "Popen", return_value=fake), \
             patch.object(proxy, "log_path", lambda: self.tmp / "server.log"), \
             patch("infra.minimal_config._pid_alive", return_value=False):
            proxy._spawn(self.tmp)
        self.assertEqual(file.read_text().strip(), "1234")
        proxy._process = None

    def test_stop_stale_leaves_a_live_parented_process_alone(self):
        """A recorded PID with a live parent belongs to another running run."""
        file = self.tmp / "open-webui.pid"
        file.write_text(str(os.getpid()))   # alive, parented by this test process
        with patch("infra.minimal_config._pid_alive", return_value=True), \
             patch("infra.minimal_config._parent_pid", return_value=os.getpid()), \
             patch("infra.minimal_config._orphaned", return_value=False), \
             patch.object(proxy, "_terminate") as terminate:
            proxy._stop_stale()
        terminate.assert_not_called()
        self.assertTrue(file.exists())


class CaddyfileTests(SimpleTestCase):
    """The generated front-door Caddyfile: a mirror of
    deploy/openwebui-subpath/Caddyfile.subpath on loopback ports."""

    def test_caddyfile_has_the_proven_auth_block(self):
        text = proxy._caddyfile(8123, 8124)
        # Front door on the public port, no TLS, no admin API.
        self.assertIn(":8123 {", text)
        self.assertIn("auto_https off", text)
        self.assertIn("admin off", text)
        # Forgery defense at site level, before anything else.
        self.assertIn("request_header -X-Studio-Email", text)
        self.assertIn("request_header -X-Studio-Name", text)
        self.assertIn("request_header -X-Studio-Role", text)
        # The forward-auth: GET to Studio /chat/authz, identity copied on 2xx,
        # 302 to login on 401 — inside the terminal handle (ordering).
        self.assertIn("method GET", text)
        self.assertIn("rewrite /chat/authz", text)
        self.assertIn("127.0.0.1:8124", text)
        self.assertIn("@good status 2xx", text)
        # The rewritten auth call drops the page query, so the embed flag
        # (admin workspace iframes) rides as a header — without it authz
        # never stamps the session and css-mask serves the plain skin.
        self.assertIn("header_up X-Studio-Embed {uri.query.embed}", text)
        self.assertIn("request_header X-Studio-Email {rp.header.X-Studio-Email}",
                      text)
        self.assertIn("@signedout status 401", text)
        # The signed-out 302 points at the browser-visible public origin.
        self.assertIn("redir http://localhost:8123/login/ 302", text)
        # The embed sheet: /chat/static/custom.css -> Studio's session-aware mask.
        self.assertIn("handle /chat/static/custom.css {", text)
        self.assertIn("rewrite /chat/css-mask", text)
        # The embed mask script: the subpath build's empty loader.js -> Studio.
        self.assertIn("handle /chat/static/loader.js {", text)
        self.assertIn("rewrite /chat/loader-mask", text)
        # Branded favicons from Studio's static/ (keys are subpath-relative).
        self.assertIn(f"handle {config.SUBPATH}/static/favicon.svg {{", text)
        self.assertIn("root *", text)
        self.assertIn("rewrite * /logo.svg", text)
        self.assertIn("file_server", text)
        # The OWUI proxy keeps the /chat path as-is (no strip) and lets the
        # iframe render it. `handle` (not `handle_path`) = no prefix strip.
        self.assertNotIn("handle_path", text)
        self.assertIn("header_down -X-Frame-Options", text)
        self.assertIn(f"handle {config.SUBPATH}/* {{", text)
        # Everything else falls through to Studio on the internal port.
        self.assertIn("handle {\n\t\treverse_proxy 127.0.0.1:8124", text)

    def test_caddyfile_owui_target_is_upstream_without_path(self):
        """OWUI is reached at its host:port only — the request path already
        carries /chat, exactly like the compose file's open-webui:8080."""
        text = proxy._caddyfile(8123, 8124)
        host, port = proxy.chat_upstream_base().replace("http://", "").split(":")
        target = f"reverse_proxy {host}:{port} {{"
        self.assertIn(target, text)
        # The auth + css-mask + loader-mask + fallback blocks all point at
        # the internal Studio port, never at the OWUI port.
        self.assertEqual(text.count("reverse_proxy 127.0.0.1:8124"), 4)
        self.assertEqual(text.count(target), 1)

    def test_caddyfile_every_branded_route_has_a_handle(self):
        text = proxy._caddyfile(8123, 8124)
        # Keys are subpath-relative, so the handle routes are absolute.
        for route in proxy._BRANDED_ASSETS:
            self.assertIn(f"handle {route} {{", text)

    def test_caddyfile_repairs_unprefixed_workspace_back_nav(self):
        # The fork's KnowledgeBase "Back" button GETs /workspace/knowledge
        # (root-relative, a missed ${base} prefix in the fork build). The
        # front door must 308 it onto the subpath — a redirect, not an
        # internal rewrite, because the SPA's client router reload-loops on
        # an out-of-base URL. The redir target must lead with a placeholder
        # ({env.WEBUI_SUBPATH}, expanded empty at spawn): a leading '/'
        # would be misparsed by caddy's caddyfile parser as a matcher, and
        # adapt would still report success.
        text = proxy._caddyfile(8123, 8124)
        self.assertIn("handle /workspace/* {", text)
        self.assertIn(
            f"redir {{env.WEBUI_SUBPATH}}{config.SUBPATH}{{http.request.uri}} 308",
            text,
        )

    def test_caddyfile_is_valid_after_adapt_when_caddy_exists(self):
        try:
            proxy._caddy_binary()
        except RuntimeError:
            self.skipTest("no caddy binary on this platform")
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Caddyfile"
            path.write_text(proxy._caddyfile(8123, 8124))
            proc = subprocess.run(
                [proxy._caddy_binary(), "adapt", "--config", str(path),
                 "--adapter", "caddyfile"],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cfg = json.loads(proc.stdout)   # valid JSON = a loadable config
        # adapt exits 0 even when the redir target is misparsed as a matcher
        # (a leading '/'), so verify the workspace back-nav actually compiles
        # to a 308 redirect — not a bogus 302 with the status word as the
        # Location.
        workspace_redirects = []
        for server in cfg["apps"]["http"]["servers"].values():
            for route in server.get("routes", []):
                matched_paths = [
                    p for m in route.get("match", []) for p in m.get("path", [])
                ]
                if not any(p.startswith("/workspace/") for p in matched_paths):
                    continue
                for handler in route.get("handle", []):
                    for sub in handler.get("routes", []):
                        for inner in sub.get("handle", []):
                            if inner.get("handler") == "static_response":
                                workspace_redirects.append(inner)
        self.assertEqual(len(workspace_redirects), 1, workspace_redirects)
        self.assertEqual(
            workspace_redirects[0]["status_code"], 308,
            workspace_redirects[0],
        )
        expected = "{env.WEBUI_SUBPATH}" + config.SUBPATH + "{http.request.uri}"
        self.assertEqual(
            workspace_redirects[0]["headers"].get("Location"), [expected],
            workspace_redirects[0],
        )

    def test_stop_caddy_is_idempotent(self):
        proxy.stop_caddy()
        proxy.stop_caddy()
        self.assertIsNone(proxy._caddy_process)


class OtlpEnvTests(SimpleTestCase):
    """When OTLP is enabled, Open WebUI is pointed at Studio's listener."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._process = proxy._process
        proxy._process = None
        self._pid_patcher = patch.object(proxy, "pid_file", lambda: self.tmp / "open-webui.pid")
        self._pid_patcher.start()
        # These tests only exercise the OTLP environment, not the launch.
        self._interp_patcher = patch.object(
            proxy, "_locate_owui_interpreter", lambda home: "/usr/bin/env python")
        self._interp_patcher.start()
        self._secret_patcher = patch.object(proxy, "_secret_key", lambda home: "test-key")
        self._secret_patcher.start()

    def tearDown(self):
        self._pid_patcher.stop()
        self._interp_patcher.stop()
        self._secret_patcher.stop()
        proxy._process = self._process
        self._tmp.cleanup()
        super().tearDown()

    def _spawn_env(self, endpoint=""):
        """Run _spawn with OTLP enabled and return the env it was given."""
        fake = MagicMock()
        fake.pid = 1234
        with patch.object(proxy.subprocess, "Popen", return_value=fake) as popen, \
             patch.object(proxy, "log_path", lambda: self.tmp / "server.log"), \
             patch("infra.minimal_config._pid_alive", return_value=False), \
             patch.object(config, "OTLP_ENABLED", True), \
             patch.object(config, "OTLP_ENDPOINT", endpoint), \
             patch.object(config, "OTLP_SERVICE_NAME", "open-webui"):
            proxy._spawn(self.tmp, 8000)
        proxy._process = None
        return popen.call_args.kwargs["env"]

    def test_disabled_by_default_adds_no_otel_vars(self):
        fake = MagicMock()
        fake.pid = 1234
        with patch.object(proxy.subprocess, "Popen", return_value=fake) as popen, \
             patch.object(proxy, "log_path", lambda: self.tmp / "server.log"), \
             patch("infra.minimal_config._pid_alive", return_value=False), \
             patch.object(config, "OTLP_ENABLED", False):
            proxy._spawn(self.tmp, 8000)
        proxy._process = None
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("ENABLE_OTEL", env)
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env)

    def test_enabled_points_at_studios_listener(self):
        env = self._spawn_env()
        self.assertEqual(env["ENABLE_OTEL"], "true")
        self.assertEqual(env["ENABLE_OTEL_TRACES"], "true")
        self.assertEqual(env["OTEL_OTLP_SPAN_EXPORTER"], "http")
        # Full ingestion URL — Open WebUI passes the endpoint to its OTLP
        # exporter explicitly, which is used as-is (no path appended).
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://127.0.0.1:8000/otlp/v1/traces")
        self.assertEqual(env["OTEL_SERVICE_NAME"], "open-webui")

    def test_explicit_endpoint_wins(self):
        # Whatever the user sets in SIMPLEAUDIT_CHAT_OTLP_ENDPOINT is passed
        # through verbatim.
        env = self._spawn_env(endpoint="http://collector:4318/otlp/v1/traces")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://collector:4318/otlp/v1/traces")


class WheelMarkerTests(SimpleTestCase):
    """The managed venv is only reused when it was built from the pinned wheel.

    Without this, an importable venv built from an older release of the same
    package would be reused forever and silently keep the old frontend. A
    missing marker (a venv that predates the check) also forces a one-time
    rebuild so every existing machine picks up the pinned wheel.
    """

    def test_default_wheel_is_content_capture_release(self):
        self.assertIn("v0.11.4.3-subpath", proxy.OWUI_WHEEL_URL)
        self.assertIn("open_webui-0.11.4.3-py3-none-any.whl", proxy.OWUI_WHEEL_URL)

    def test_install_requirement_includes_observability_extra(self):
        requirement = proxy._owui_install_requirement()
        self.assertIn("open-webui[observability]", requirement)
        self.assertIn(proxy.OWUI_WHEEL_URL, requirement)

    def test_missing_marker_is_a_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(proxy._managed_venv_matches_wheel(Path(tmp)))

    def test_matching_wheel_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = Path(tmp)
            proxy._wheel_marker(managed).write_text(proxy.OWUI_WHEEL_URL)
            self.assertTrue(proxy._managed_venv_matches_wheel(managed))

    def test_stale_wheel_triggers_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = Path(tmp)
            proxy._wheel_marker(managed).write_text("https://example.com/old.whl")
            self.assertFalse(proxy._managed_venv_matches_wheel(managed))
