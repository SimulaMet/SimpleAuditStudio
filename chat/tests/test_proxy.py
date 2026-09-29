"""The forward-auth proxy, against a stub Studio and a stub Open WebUI.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test chat.tests.test_proxy
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase

from chat import config, proxy

VALID_COOKIE = "sessionid=good"


class _StubStudio(BaseHTTPRequestHandler):
    """Answers /chat/authz, and sets a cookie the way Django does."""

    protocol_version = "HTTP/1.1"
    calls = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        # Substring, not equality: a browser (or a leaking proxy) sends several
        # cookies, and an exact match would quietly answer 401 to a request that
        # really does carry the session.
        type(self).calls += 1
        signed_in = VALID_COOKIE in (self.headers.get("Cookie") or "")
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

    def setUp(self):
        proxy._identity_cache.clear()
        _StubStudio.calls = 0

    def test_a_signed_in_browser_is_identified(self):
        response = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertEqual(response.json()["email"], "ada@example.com")
        self.assertEqual(response.json()["role"], "admin")

    def test_a_client_cannot_supply_its_own_identity(self):
        """In any casing: HTTP header names are case-insensitive.

        Overwriting the headers we forward is not enough on its own. A client
        sending `x-studio-email` in another casing would add a second header
        rather than replace ours, and the upstream reads whichever comes first.
        """
        for name in (config.EMAIL_HEADER, config.EMAIL_HEADER.lower(), config.EMAIL_HEADER.upper()):
            response = httpx.get(f"{self.url}/api/config", headers={
                "Cookie": VALID_COOKIE, name: "evil@example.com",
            }).json()
            self.assertEqual(response["email"], "ada@example.com", name)
            self.assertEqual(response["identity_headers_seen"], 1, name)

    def test_a_signed_out_browser_gets_a_way_back_not_the_chat(self):
        response = httpx.get(self.url, follow_redirects=False)
        self.assertIn("Sign in to Studio", response.text)
        self.assertIn("top.location", response.text)   # breaks out of the iframe

    def test_one_browsers_session_never_reaches_another(self):
        """The proxy must remember nothing between requests.

        This is the bug that made a cookieless request come back as the last
        signed-in user: an httpx client keeps a cookie jar, so Studio's sessionid
        and Open WebUI's token were stored and replayed for whoever asked next.
        """
        signed_in = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertEqual(signed_in.json()["email"], "ada@example.com")

        anonymous = httpx.get(f"{self.url}/api/config")
        self.assertIn("Sign in to Studio", anonymous.text)

    def test_the_leak_is_what_the_test_above_would_catch(self):
        """The control: put the old shared client back, and the leak reappears.

        Without this, the test above passes for any reason at all — including a
        broken stub — and would not notice the bug coming back.
        """
        shared = httpx.Client(transport=proxy._Handler.transport, follow_redirects=False)
        with patch.object(proxy._Handler, "client", lambda self: shared):
            httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
            anonymous = httpx.get(f"{self.url}/api/config")
        self.assertEqual(anonymous.json()["email"], "ada@example.com",
                         "the shared client no longer leaks — has httpx changed?")

    def test_every_request_gets_a_client_that_remembers_nothing(self):
        """The property the fix rests on, asserted without going through HTTP."""
        handler = proxy._Handler.__new__(proxy._Handler)
        first, second = handler.client(), handler.client()
        self.assertIsNot(first, second)
        first.cookies.set("sessionid", "someone-elses")
        self.assertEqual(dict(handler.client().cookies), {})

    def test_the_upstreams_cookies_are_not_kept_either(self):
        httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        second = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertNotIn("someones-jwt", second.json()["cookie_seen"] or "")

    def test_a_page_load_asks_studio_once_not_once_per_asset(self):
        """Open WebUI pulls dozens of assets; each one asking Django would show."""
        for _ in range(5):
            httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertEqual(_StubStudio.calls, 1)

    def test_the_answer_stops_being_used_once_it_is_old(self):
        """Otherwise a sign-out would never take effect."""
        with patch.object(proxy, "IDENTITY_TTL", 0.05):
            httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
            time.sleep(0.1)
            httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertEqual(_StubStudio.calls, 2)

    def test_the_cache_can_be_turned_off(self):
        with patch.object(proxy, "IDENTITY_TTL", 0):
            for _ in range(3):
                httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertEqual(_StubStudio.calls, 3)

    def test_the_frame_blocking_header_is_removed(self):
        response = httpx.get(f"{self.url}/api/config", headers={"Cookie": VALID_COOKIE})
        self.assertNotIn("x-frame-options", response.headers)
