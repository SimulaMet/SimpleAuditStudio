"""The forward-auth proxy, against a stub Studio and a stub Open WebUI.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test chat.tests.test_proxy
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase

from chat import config, proxy

VALID_COOKIE = "sessionid=good"


class _StubStudio(BaseHTTPRequestHandler):
    """Answers /chat/authz, and sets a cookie the way Django does."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        signed_in = self.headers.get("Cookie") == VALID_COOKIE
        self.send_response(200 if signed_in else 401)
        if signed_in:
            self.send_header(config.EMAIL_HEADER, "ada@example.com")
            self.send_header(config.NAME_HEADER, "Ada")
            self.send_header(config.ROLE_HEADER, "admin")
        # Django sets cookies on these replies (csrftoken, and sessionid when
        # the session is touched). A proxy that keeps them would hand them to
        # the next browser.
        self.send_header("Set-Cookie", f"{VALID_COOKIE}; Path=/")
        self.send_header("Set-Cookie", "csrftoken=abc; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()


class _StubOpenWebUI(BaseHTTPRequestHandler):
    """Echoes the identity it was given, and sets a session token cookie."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({
            "email": self.headers.get(config.EMAIL_HEADER),
            "role": self.headers.get(config.ROLE_HEADER),
            "cookie_seen": self.headers.get("Cookie"),
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


class ProxyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.studio = _serve(_StubStudio)
        cls.upstream = _serve(_StubOpenWebUI)
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream.server_address[1]}"
        cls.patcher = patch.object(config, "UPSTREAM", cls.upstream_url)
        cls.patcher.start()
        cls.port_patcher = patch.object(config, "PROXY_PORT", 0)
        cls.port_patcher.start()
        cls.server = proxy.serve(cls.studio.server_address[1])
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.studio.shutdown()
        cls.upstream.shutdown()
        cls.port_patcher.stop()
        cls.patcher.stop()
        super().tearDownClass()

    def test_a_signed_in_browser_is_identified(self):
        response = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertEqual(response.json()["email"], "ada@example.com")
        self.assertEqual(response.json()["role"], "admin")

    def test_client_supplied_identity_is_dropped(self):
        response = httpx.get(f"{self.url}/api/config", headers={
            "Cookie": VALID_COOKIE,
            config.EMAIL_HEADER: "evil@example.com",
            config.ROLE_HEADER: "admin",
        })
        self.assertEqual(response.json()["email"], "ada@example.com")

    def test_a_signed_out_browser_gets_a_way_back_not_the_chat(self):
        response = httpx.get(self.url, follow_redirects=False)
        self.assertIn("Sign in to Studio", response.text)
        self.assertIn("top.location", response.text)   # breaks out of the iframe

    def test_one_browsers_session_never_reaches_another(self):
        """The proxy must remember nothing between requests.

        A client with a cookie jar would keep the Set-Cookie headers from the
        stubs above — Studio's sessionid and Open WebUI's token — and send them
        on the next request, identifying an anonymous browser as the last user.
        """
        signed_in = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertEqual(signed_in.json()["email"], "ada@example.com")

        anonymous = httpx.get(f"{self.url}/api/config")
        self.assertIn("Sign in to Studio", anonymous.text)

    def test_the_upstreams_cookies_are_not_kept_either(self):
        httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        second = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertNotIn("someones-jwt", second.json()["cookie_seen"] or "")

    def test_the_frame_blocking_header_is_removed(self):
        response = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertNotIn("x-frame-options", response.headers)
