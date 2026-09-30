"""Integration tests for local demo mode (SIMPLEAUDIT_MINIMAL=1).

These tests verify that the one-liner `uvx simpleaudit-studio` boot sequence
works correctly: settings, migrations, seeding, embedded Hatchet, mock server,
and the web UI banner.

Run with:  SIMPLEAUDIT_LOCAL_SQLITE=1 python manage.py test infra.tests.test_minimal_config
"""

from __future__ import annotations

import os
import urllib.request
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings, tag

User = get_user_model()


class TestDemoBootSequence(TestCase):
    """Verify the seed + mock server components work in isolation."""

    def test_bootstrap_and_seed(self):
        """Bootstrap admin + seed creates model endpoints."""
        from django.core.management import call_command

        from accounts.services import bootstrap_admin_and_default_project
        from model_registry.models import ModelConnection

        user, project = bootstrap_admin_and_default_project(
            username="studio",
            email="admin@localhost",
            password="admin12345",
            project_name="Demo Project",
        )
        self.assertEqual(user.username, "studio")
        self.assertEqual(project.name, "Demo Project")

        call_command("seed_platform", project=project.id, verbosity=0)
        self.assertGreater(ModelConnection.objects.filter(enabled=True).count(), 0)

    def test_mock_server_start_stop(self):
        """Mock OpenAI server starts on a random port and responds to healthz."""
        from deploy.mock_openai_server import start_mock_server, stop_mock_server

        server, port = start_mock_server(port=0)
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
            self.assertEqual(resp.status, 200)
        finally:
            stop_mock_server(server)

    def test_model_endpoints_point_at_openai_after_seed(self):
        """After seeding, model connections point at OpenAI's real base URL."""
        from django.core.management import call_command

        from accounts.services import bootstrap_admin_and_default_project
        from model_registry.models import ModelConnection

        _, project = bootstrap_admin_and_default_project(
            username="studio", email="admin@localhost", password="admin12345",
            project_name="Demo Project",
        )
        call_command("seed_platform", project=project.id, verbosity=0)

        conn = ModelConnection.objects.filter(enabled=True).first()
        self.assertIsNotNone(conn)
        self.assertEqual(conn.base_url, "https://api.openai.com/v1")

    def test_restore_real_model_endpoints_repairs_mock(self):
        """A connection left pointing at the local mock is repaired to OpenAI."""
        from django.core.management import call_command

        from accounts.services import bootstrap_admin_and_default_project
        from model_registry.models import ModelConnection
        from simpleaudit_studio.cli import _restore_real_model_endpoints

        _, project = bootstrap_admin_and_default_project(
            username="studio", email="admin@localhost", password="admin12345",
            project_name="Demo Project",
        )
        call_command("seed_platform", project=project.id, verbosity=0)

        # Simulate an earlier version that left the connection at the mock.
        ModelConnection.objects.filter(enabled=True).update(
            base_url="http://127.0.0.1:49615/v1",
        )
        _restore_real_model_endpoints()

        conn = ModelConnection.objects.filter(enabled=True).first()
        self.assertIsNotNone(conn)
        self.assertEqual(conn.base_url, "https://api.openai.com/v1")



@tag("embedded_hatchet")
class TestEmbeddedHatchetLifecycle(TestCase):
    """Test the embedded Hatchet start/stop cycle.

    These require the sidecar binary and network access. The engine is started
    once for the whole class (setUpClass) and stopped once (tearDownClass), so
    the ~16s cost is paid a single time instead of per-test.

    Tagged 'embedded_hatchet' so CI can exclude them from parallel runs
    (the sidecar cannot start in multiple subprocesses simultaneously).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from infra.minimal_config import start_embedded_hatchet

        cls._hatchet_client = start_embedded_hatchet()

    @classmethod
    def tearDownClass(cls):
        from infra.minimal_config import stop_embedded_hatchet

        stop_embedded_hatchet()
        super().tearDownClass()

    def test_data_dir_is_persistent(self):
        """The embedded Postgres data dir is a fixed location, not a temp dir."""
        from infra.minimal_config import _data_dir

        d1 = _data_dir()
        d2 = _data_dir()
        self.assertEqual(d1, d2)
        self.assertTrue(os.path.isdir(d1))
        # Must not be a per-run temp dir (the old leak: 37 x 218M in $TMPDIR)
        self.assertNotIn("simpleaudit-hatchet-pg-", os.path.basename(d1))

    def test_data_dir_env_override(self):
        """SIMPLEAUDIT_EMBEDDED_PG_DIR overrides the default location."""

        from infra.minimal_config import _data_dir

        with patch.dict(os.environ, {"SIMPLEAUDIT_EMBEDDED_PG_DIR": "/tmp/custom-pg-dir-test"}):
            d = _data_dir()
        self.assertEqual(d, "/tmp/custom-pg-dir-test")
        self.assertTrue(os.path.isdir(d))
        import shutil
        shutil.rmtree("/tmp/custom-pg-dir-test", ignore_errors=True)

    def test_start_and_stop(self):
        """The shared client started in setUpClass is live and usable."""
        self.assertIsNotNone(self._hatchet_client)

    def test_get_client_returns_embedded(self):
        """In minimal config, worker.get_client() returns the embedded client."""
        with patch.dict(os.environ, {"SIMPLEAUDIT_MINIMAL": "1"}):
            import infra.worker as w
            from infra.worker import get_client

            w._CLIENT = None
            try:
                c = get_client()
                self.assertIs(c, self._hatchet_client)
            finally:
                w._CLIENT = None


class TestAutoLoginToken(TestCase):
    """The /auto-login/ endpoint must be one-time: a single-use token URL.

    The demo server binds 0.0.0.0, so the token is what keeps other machines
    on the LAN from signing in as the bootstrap user.
    """

    TOKEN = "test-one-time-token-123"

    def setUp(self):
        self.user = User.objects.create_user(username="studio", password="x")

    def tearDown(self):
        os.environ.pop("SIMPLEAUDIT_AUTO_LOGIN_TOKEN", None)

    def _login(self, token=None):
        url = "/auto-login/"
        if token is not None:
            url += f"?token={token}"
        # follow=True: the view 302s to the dashboard after signing in.
        return self.client.get(url, follow=True)

    @override_settings(MINIMAL_CONFIG=True)
    def test_valid_token_logs_in_and_consumes(self):
        with patch.dict(os.environ, {"SIMPLEAUDIT_AUTO_LOGIN_TOKEN": self.TOKEN}):
            response = self._login(self.TOKEN)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.wsgi_request.user.username, "studio")
            # The token is single-use: it must be gone after the first login.
            self.assertNotIn("SIMPLEAUDIT_AUTO_LOGIN_TOKEN", os.environ)

    @override_settings(MINIMAL_CONFIG=True)
    def test_token_cannot_be_replayed(self):
        with patch.dict(os.environ, {"SIMPLEAUDIT_AUTO_LOGIN_TOKEN": self.TOKEN}):
            first = self._login(self.TOKEN)
            self.assertEqual(first.status_code, 200)
            # Second use of the same URL: token already consumed -> 404.
            second = self._login(self.TOKEN)
        self.assertEqual(second.status_code, 404)

    @override_settings(MINIMAL_CONFIG=True)
    def test_invalid_token_rejected(self):
        with patch.dict(os.environ, {"SIMPLEAUDIT_AUTO_LOGIN_TOKEN": self.TOKEN}):
            response = self._login("wrong-token")
            self.assertEqual(response.status_code, 404)
            # Token must survive a failed attempt so the real URL still works.
            self.assertEqual(os.environ.get("SIMPLEAUDIT_AUTO_LOGIN_TOKEN"), self.TOKEN)

    @override_settings(MINIMAL_CONFIG=True)
    def test_missing_token_rejected(self):
        with patch.dict(os.environ, {"SIMPLEAUDIT_AUTO_LOGIN_TOKEN": self.TOKEN}):
            response = self._login()
        self.assertEqual(response.status_code, 404)

    @override_settings(MINIMAL_CONFIG=True)
    def test_no_token_configured_rejects_everything(self):
        os.environ.pop("SIMPLEAUDIT_AUTO_LOGIN_TOKEN", None)
        response = self._login(self.TOKEN)
        self.assertEqual(response.status_code, 404)

    @override_settings(MINIMAL_CONFIG=False)
    def test_disabled_outside_minimal_config(self):
        with patch.dict(os.environ, {"SIMPLEAUDIT_AUTO_LOGIN_TOKEN": self.TOKEN}):
            response = self._login(self.TOKEN)
        self.assertEqual(response.status_code, 404)
