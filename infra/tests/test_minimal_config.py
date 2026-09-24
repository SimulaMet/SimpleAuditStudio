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

from django.test import TestCase, override_settings


class TestDemoBootSequence(TestCase):
    """Verify the seed + mock server components work in isolation."""

    def test_bootstrap_and_seed(self):
        """Bootstrap admin + seed creates model endpoints."""
        from accounts.services import bootstrap_admin_and_default_project
        from django.core.management import call_command
        from model_registry.models import ModelEndpoint

        user, project = bootstrap_admin_and_default_project(
            username="studio",
            email="admin@localhost",
            password="admin12345",
            project_name="Demo Project",
        )
        self.assertEqual(user.username, "studio")
        self.assertEqual(project.name, "Demo Project")

        call_command("seed_platform", project=project.id, verbosity=0)
        self.assertGreater(ModelEndpoint.objects.filter(enabled=True).count(), 0)

    def test_mock_server_start_stop(self):
        """Mock OpenAI server starts on a random port and responds to healthz."""
        from deploy.mock_openai_server import start_mock_server, stop_mock_server

        server, port = start_mock_server(port=0)
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
            self.assertEqual(resp.status, 200)
        finally:
            stop_mock_server(server)

    def test_model_endpoints_updated_to_mock(self):
        """After pointing endpoints at the mock server, they have api_key_direct set."""
        from accounts.services import bootstrap_admin_and_default_project
        from deploy.mock_openai_server import start_mock_server, stop_mock_server
        from django.core.management import call_command
        from model_registry.models import ModelEndpoint

        _, project = bootstrap_admin_and_default_project(
            username="studio", email="admin@localhost", password="admin12345",
            project_name="Demo Project",
        )
        call_command("seed_platform", project=project.id, verbosity=0)

        server, port = start_mock_server(port=0)
        try:
            mock_url = f"http://127.0.0.1:{port}/v1"
            ModelEndpoint.objects.filter(enabled=True).update(
                base_url=mock_url,
                api_key_direct="mock-key",
                secret_reference="",
            )
            conn = ModelEndpoint.objects.filter(enabled=True).first()
            self.assertIsNotNone(conn)
            self.assertEqual(conn.base_url, mock_url)
            self.assertEqual(conn.api_key_direct, "mock-key")
        finally:
            stop_mock_server(server)



class TestEmbeddedHatchetLifecycle(TestCase):
    """Test the embedded Hatchet start/stop cycle.

    These require the sidecar binary and network access. They will be slow
    (~15s first run) but verify the full lifecycle works.
    """

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
        import tempfile as _tempfile

        from infra.minimal_config import _data_dir

        with patch.dict(os.environ, {"SIMPLEAUDIT_EMBEDDED_PG_DIR": "/tmp/custom-pg-dir-test"}):
            d = _data_dir()
        self.assertEqual(d, "/tmp/custom-pg-dir-test")
        self.assertTrue(os.path.isdir(d))
        import shutil
        shutil.rmtree("/tmp/custom-pg-dir-test", ignore_errors=True)

    def test_start_and_stop(self):
        from infra.minimal_config import start_embedded_hatchet, stop_embedded_hatchet

        client = start_embedded_hatchet()
        self.assertIsNotNone(client)
        stop_embedded_hatchet()

    def test_get_client_returns_embedded(self):
        from infra.minimal_config import start_embedded_hatchet, stop_embedded_hatchet

        with patch.dict(os.environ, {"SIMPLEAUDIT_MINIMAL": "1"}):
            client = start_embedded_hatchet()
            try:
                from infra.worker import get_client

                import infra.worker as w
                w._CLIENT = None
                c = get_client()
                self.assertIs(c, client)
            finally:
                stop_embedded_hatchet()
                import infra.worker as w
                w._CLIENT = None
