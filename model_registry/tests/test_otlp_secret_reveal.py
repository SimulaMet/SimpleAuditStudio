"""OTLP secrets can be revealed again, saving never rotates, and auth is cached."""
from unittest import mock

from django.db import connection as db_connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    UserFactory,
)
from model_registry import otlp_services as otlp
from model_registry.models import OTLPCredential

PW = "testpass" + "123"


class SecretRevealServiceTest(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.conn = ModelConnectionFactory(project=self.project, name="reveal")

    def test_reveal_returns_issued_secret_for_each_mode(self):
        for mode in ("basic", "bearer"):
            nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode=mode)
            self.assertEqual(otlp.reveal_secret(nc.credential), nc.secret)
            self.assertNotIn(nc.secret, nc.credential.secret_encrypted)

    def test_rotate_replaces_the_revealable_secret(self):
        nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer")
        rotated = otlp.rotate_credential(nc.credential)
        self.assertEqual(otlp.reveal_secret(rotated.credential), rotated.secret)
        self.assertNotEqual(rotated.secret, nc.secret)

    def test_legacy_and_none_credentials_have_nothing_to_reveal(self):
        nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer")
        OTLPCredential.objects.filter(pk=nc.credential.pk).update(secret_encrypted="")
        self.assertIsNone(otlp.reveal_secret(OTLPCredential.objects.get(pk=nc.credential.pk)))
        none = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="none")
        self.assertIsNone(otlp.reveal_secret(none.credential))

    def test_reveal_after_secret_key_change_is_none(self):
        nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer")
        with self.settings(SECRET_KEY="another-key"):
            self.assertIsNone(otlp.reveal_secret(nc.credential))


class OTLPDialogEndpointsTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.user.set_password(PW)
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="dialog")
        self.client = Client(SERVER_NAME="localhost")
        self.client.login(username=self.user.username, password=PW)

    def _create(self, mode="bearer"):
        return self.client.post(
            "/connections/otlp-credential/", {"connection_id": self.conn.id, "auth_mode": mode},
        )

    def test_saving_same_mode_again_shows_the_same_secret(self):
        first = self._create().json()
        second = self._create().json()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(first["revealed"])
        self.assertTrue(second["revealed"])
        self.assertEqual(first["secret"], second["secret"])
        self.assertEqual(OTLPCredential.objects.filter(connection=self.conn).count(), 1)

    def test_legacy_secret_asks_for_rotation_instead_of_rotating(self):
        first = self._create().json()
        OTLPCredential.objects.filter(connection=self.conn).update(secret_encrypted="")
        resp = self._create()
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["needs_rotate"])
        # the old secret still works
        self.assertIsNotNone(otlp.verify_bearer(first["secret"]))

    def test_switching_mode_issues_a_new_secret(self):
        first = self._create("bearer").json()
        second = self._create("basic").json()
        self.assertEqual(second["credential"]["auth_mode"], "basic")
        self.assertFalse(second["revealed"])
        self.assertIsNone(otlp.verify_bearer(first["secret"]))

    def test_non_admin_cannot_create(self):
        outsider = UserFactory()
        outsider.set_password(PW)
        outsider.save()
        MembershipFactory(user=outsider, project=self.project, role="viewer")
        client = Client(SERVER_NAME="localhost")
        client.login(username=outsider.username, password=PW)
        resp = client.post("/connections/otlp-credential/", {"connection_id": self.conn.id, "auth_mode": "bearer"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(OTLPCredential.objects.filter(connection=self.conn).exists())


class OTLPAuthCacheTest(TestCase):
    def setUp(self):
        otlp.clear_auth_cache()
        self.project = ProjectFactory()
        self.conn = ModelConnectionFactory(project=self.project, name="cache")
        self.nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer")
        self.header = f"Bearer {self.nc.secret}"
        otlp.clear_auth_cache()

    def test_repeat_exports_cost_no_queries(self):
        first = otlp.authenticate(self.header)
        self.assertEqual(first.target_id, self.nc.credential.target_id)
        with CaptureQueriesContext(db_connection) as ctx:
            again = otlp.authenticate(self.header)
        self.assertEqual(again, first)
        self.assertEqual(len(ctx), 0)

    def test_cold_lookup_is_one_query(self):
        with CaptureQueriesContext(db_connection) as ctx:
            self.assertIsNotNone(otlp.authenticate(self.header))
        self.assertEqual(len(ctx), 1)

    def test_unknown_token_is_one_query_then_cached(self):
        with CaptureQueriesContext(db_connection) as ctx:
            self.assertIsNone(otlp.authenticate("Bearer sa_otlp_nope"))
            self.assertIsNone(otlp.authenticate("Bearer sa_otlp_nope"))
        self.assertEqual(len(ctx), 1)

    def test_revoke_and_rotate_take_effect_at_once(self):
        self.assertIsNotNone(otlp.authenticate(self.header))
        self.nc.credential.enabled = False
        self.nc.credential.save()
        self.assertIsNone(otlp.authenticate(self.header))
        rotated = otlp.rotate_credential(self.nc.credential)
        self.assertIsNone(otlp.authenticate(self.header))
        self.assertIsNotNone(otlp.authenticate(f"Bearer {rotated.secret}"))

    def test_cache_can_be_switched_off(self):
        with mock.patch.object(otlp, "AUTH_CACHE_SECONDS", 0):
            otlp.authenticate(self.header)
            with CaptureQueriesContext(db_connection) as ctx:
                otlp.authenticate(self.header)
        self.assertGreaterEqual(len(ctx), 1)
