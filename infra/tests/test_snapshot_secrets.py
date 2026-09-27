"""Run snapshots never hold raw API keys; the worker resolves keys at execution time."""
import importlib
import os
from unittest import mock

from django.apps import apps
from django.test import Client, TestCase

from audits.services import _endpoint_snapshot
from infra.engine import _validate_secrets, snapshot_api_key
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)

SECRET = "sk-super-secret-123"


class SnapshotSecretTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.conn = ModelConnectionFactory(project=self.project, api_key_direct=SECRET)
        self.model = RegisteredModelFactory(connection=self.conn)

    def test_snapshot_has_no_key_but_links_the_connection(self):
        snap = _endpoint_snapshot(self.model)
        self.assertNotIn("api_key_direct", snap)
        self.assertNotIn(SECRET, str(snap))
        self.assertEqual(snap["connection_id"], self.conn.id)

    def test_key_resolved_from_connection_at_execution_time(self):
        snap = _endpoint_snapshot(self.model)
        self.assertEqual(snapshot_api_key(snap), SECRET)
        self.conn.api_key_direct = "sk-rotated"   # rotating a key applies to queued runs too
        self.conn.save()
        self.assertEqual(snapshot_api_key(snap), "sk-rotated")

    def test_env_reference_and_deleted_connection(self):
        with mock.patch.dict(os.environ, {"MY_KEY": "from-env"}):
            self.assertEqual(snapshot_api_key({"connection_id": 999999, "secret_reference": "MY_KEY"}), "from-env")
            self.conn.api_key_direct = ""
            self.conn.secret_reference = "MY_KEY"
            self.conn.save()
            self.assertEqual(snapshot_api_key(_endpoint_snapshot(self.model)), "from-env")
        self.assertIsNone(snapshot_api_key({"secret_reference": ""}))

    def test_validate_accepts_connection_key_without_env(self):
        self.conn.secret_reference = "UNSET_ENV_VAR_XYZ"
        self.conn.save()
        _validate_secrets(("target", _endpoint_snapshot(self.model)))   # stored key is enough

    def test_runs_api_does_not_expose_keys(self):
        user = UserFactory()
        MembershipFactory(user=user, project=self.project, role="viewer")
        run = AuditRunFactory(project=self.project, target_model=self.model,
                              target_config_snapshot=_endpoint_snapshot(self.model))
        client = Client()
        client.force_login(user)
        body = client.get(f"/api/projects/{self.project.id}/audit-runs/{run.id}/").content.decode()
        self.assertNotIn(SECRET, body)


class StripKeysMigrationTests(TestCase):
    def test_existing_snapshots_are_cleaned(self):
        conn = ModelConnectionFactory(api_key_direct=SECRET)
        model = RegisteredModelFactory(connection=conn, project=conn.project)
        old = {"id": model.id, "base_url": "http://x/v1", "api_key_direct": SECRET, "secret_reference": ""}
        run = AuditRunFactory(project=conn.project, target_config_snapshot=old, auditor_config_snapshot=old,
                              judge_config_snapshot={"id": 0, "api_key_direct": SECRET})
        migration = importlib.import_module("audits.migrations.0002_strip_api_keys_from_snapshots")
        migration.strip_keys(apps, None)
        run.refresh_from_db()
        for snap in (run.target_config_snapshot, run.auditor_config_snapshot, run.judge_config_snapshot):
            self.assertNotIn("api_key_direct", snap)
        self.assertEqual(run.target_config_snapshot["connection_id"], conn.id)
