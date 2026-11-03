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


class _DialogBase(TestCase):
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


class OTLPDialogEndpointsTest(_DialogBase):

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


class OTLPStatusEndpointTest(_DialogBase):
    def _status(self, cred_id):
        return self.client.get("/connections/otlp-credential/status/", {"credential_id": cred_id})

    def test_status_reports_nothing_before_any_export(self):
        cred = self._create().json()["credential"]
        body = self._status(cred["id"]).json()
        self.assertTrue(body["ok"] and body["enabled"])
        self.assertIsNone(body["last_at"])
        self.assertEqual(body["exports"], 0)

    def test_status_sees_filtered_export(self):
        from model_registry import otlp_views

        cred = self._create().json()["credential"]
        target = OTLPCredential.objects.get(pk=cred["id"]).target_id
        otlp_views.note_export(target, received=3, kept=0)
        body = self._status(cred["id"]).json()
        self.assertIsNotNone(body["last_at"])
        self.assertEqual((body["exports"], body["spans_received"], body["spans_kept"]), (1, 3, 0))

    def test_status_is_workspace_scoped(self):
        other = OTLPCredential.objects.create(
            project=ProjectFactory(), connection=ModelConnectionFactory(), target_id="x", auth_mode="none",
        )
        self.assertEqual(self._status(other.id).status_code, 404)


class OTLPRoundTripTest(_DialogBase):
    def _test(self, cred_id, **extra):
        return self.client.post("/connections/otlp-credential/test/", {"credential_id": cred_id, **extra})

    def _cred(self, mode="bearer"):
        return OTLPCredential.objects.get(pk=self._create(mode).json()["credential"]["id"])

    def test_receiver_check_passes_for_each_mode_and_leaves_no_trace(self):
        from model_registry import otlp_views
        from model_registry.models import OtlpSpan

        for mode in ("basic", "bearer", "none"):
            # one credential per connection: each mode replaces the last
            cred = (
                otlp.create_credential(project=self.project, connection=self.conn, auth_mode=mode).credential
            )
            self.assertTrue(otlp_views.round_trip_test(cred)["ok"], cred.auth_mode)
            self.assertIsNone(otlp_views.last_export(cred.target_id))
            self.assertFalse(OtlpSpan.objects.filter(target_id=cred.target_id, name="studio.otlp.test").exists())

    def _exporting_send(self, cred, linked=True):
        """A target that replies and exports one LLM span (under the sent trace id if linked)."""
        import json as jsonlib

        secret = otlp.reveal_secret(cred)

        def send(traceparent):
            trace_id = traceparent.split("-")[1] if linked else "ab" * 16
            span = {"traceId": trace_id, "spanId": "cd" * 8, "name": "llm", "startTimeUnixNano": "1",
                    "endTimeUnixNano": "2", "attributes": [{"key": "openinference.span.kind", "value": {"stringValue": "LLM"}}]}
            resp = Client(SERVER_NAME="localhost").post(
                "/otlp/v1/traces", data=jsonlib.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}),
                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {secret}",
            )
            assert resp.status_code == 200
            return "pong"

        return send

    def test_chat_round_trip_links_the_trace(self):
        from model_registry import otlp_views

        cred = self._cred()
        out = otlp_views.chat_round_trip(cred, self._exporting_send(cred), wait=0)
        self.assertEqual((out["ok"], out["stage"], out["spans"]), (True, "linked", 1))

    def test_chat_round_trip_without_traceparent_adoption_is_arrived(self):
        from model_registry import otlp_views

        cred = self._cred()
        out = otlp_views.chat_round_trip(cred, self._exporting_send(cred, linked=False), wait=0)
        self.assertEqual((out["ok"], out["stage"]), (True, "arrived"))

    def test_chat_round_trip_reports_missing_trace_and_failed_call(self):
        from model_registry import otlp_views

        cred = self._cred()
        self.assertEqual(otlp_views.chat_round_trip(cred, lambda tp: "pong", wait=0)["stage"], "no_trace")

        def boom(tp):
            raise RuntimeError("model down")

        failed = otlp_views.chat_round_trip(cred, boom, wait=0)
        self.assertEqual(failed["stage"], "call")
        self.assertIn("model down", failed["detail"])

    def test_endpoint_without_callable_model_reports_after_receiver_check(self):
        cred = self._cred()
        body = self._test(cred.id).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["stage"], "call")

    def test_legacy_credential_without_secret_asks_for_rotation(self):
        cred = self._cred()
        OTLPCredential.objects.filter(pk=cred.pk).update(secret_encrypted="")
        self.assertEqual(self._test(cred.id).json()["stage"], "secret")

    def test_disabled_credential_fails(self):
        cred = self._cred()
        OTLPCredential.objects.filter(pk=cred.pk).update(enabled=False)
        self.assertEqual(self._test(cred.id).json()["stage"], "disabled")

    def test_viewer_cannot_run_test(self):
        cred = self._cred()
        viewer = UserFactory()
        viewer.set_password(PW)
        viewer.save()
        MembershipFactory(user=viewer, project=self.project, role="viewer")
        client = Client(SERVER_NAME="localhost")
        client.login(username=viewer.username, password=PW)
        self.assertEqual(client.post("/connections/otlp-credential/test/", {"credential_id": cred.id}).status_code, 403)
