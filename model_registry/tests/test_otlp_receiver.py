"""Tests for the shared OTLP receiver: credential services + ingestion endpoint.

Covers the two auth modes (basic / bearer), header parsing, span tagging,
auth failures, method handling, and revocation.
"""
import base64
import json
from unittest import mock

from django.test import Client, TestCase
from simpleaudit.tracing.auth import parse_basic_header, parse_bearer_header

from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    UserFactory,
)
from model_registry import otlp_services as otlp
from model_registry import otlp_views
from model_registry.models import OTLPCredential


def _basic_header(username, password):
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _otlp_body():
    return json.dumps(
        {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "ab" * 16, "spanId": "cd" * 8, "name": "turn"}]}]}]}
    )


class OTLPHeaderParsingTest(TestCase):
    def test_parse_basic(self):
        self.assertEqual(parse_basic_header(_basic_header("u", "p")), ("u", "p"))

    def test_parse_basic_with_colon_in_password(self):
        self.assertEqual(parse_basic_header(_basic_header("u", "a:b:c")), ("u", "a:b:c"))

    def test_parse_bearer(self):
        self.assertEqual(parse_bearer_header("Bearer tok123"), "tok123")

    def test_scheme_mismatch_returns_none(self):
        self.assertIsNone(parse_basic_header("Bearer x"))
        self.assertIsNone(parse_bearer_header("Basic x"))

    def test_missing_header(self):
        self.assertIsNone(parse_basic_header(None))
        self.assertIsNone(parse_bearer_header(""))

    def test_invalid_base64(self):
        self.assertIsNone(parse_basic_header("Basic !!!not-base64!!!"))


class OTLPCredentialServiceTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        self.conn = ModelConnectionFactory(project=self.project, name="owui-a")

    def test_create_basic_returns_secret_and_stores_hash(self):
        nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="basic", user=self.user)
        self.assertTrue(nc.secret)
        self.assertEqual(nc.credential.auth_mode, OTLPCredential.AuthMode.BASIC)
        self.assertTrue(nc.credential.username.startswith("sa_"))
        # The plaintext is not stored; only a hash.
        self.assertNotEqual(nc.credential.secret_hash, nc.secret.encode())
        # Round-trip verification succeeds.
        self.assertIsNotNone(otlp.verify_basic(nc.credential.username, nc.secret))

    def test_create_bearer_returns_token(self):
        nb = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer", user=self.user)
        self.assertTrue(nb.secret.startswith("sa_otlp_"))
        self.assertEqual(nb.credential.username, "")
        self.assertIsNotNone(otlp.verify_bearer(nb.secret))

    def test_verify_wrong_password_returns_none(self):
        nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="basic", user=self.user)
        self.assertIsNone(otlp.verify_basic(nc.credential.username, "wrong"))

    def test_verify_wrong_bearer_returns_none(self):
        self.assertIsNone(otlp.verify_bearer("sa_otlp_not_a_real_token"))

    def test_bearer_stores_lookup_prefix(self):
        nb = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer", user=self.user)
        self.assertEqual(nb.credential.token_prefix, otlp.token_lookup_prefix(nb.secret))
        # A token sharing the prefix but differing in the hash body must not match.
        fake = nb.secret[:16] + "0" * (len(nb.secret) - 16)
        self.assertNotEqual(fake, nb.secret)
        self.assertIsNone(otlp.verify_bearer(fake))

    def test_bearer_legacy_row_without_prefix_still_verifies(self):
        """Rows created before token_prefix existed (empty prefix) still verify via fallback."""
        nb = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer", user=self.user)
        nb.credential.token_prefix = ""
        nb.credential.save(update_fields=["token_prefix"])
        self.assertIsNotNone(otlp.verify_bearer(nb.secret))

    def test_disabled_credential_not_verified(self):
        nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="basic", user=self.user)
        nc.credential.enabled = False
        nc.credential.save()
        self.assertIsNone(otlp.verify_basic(nc.credential.username, nc.secret))

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            otlp.create_credential(project=self.project, connection=self.conn, auth_mode="mtls")

    def test_create_none_has_no_secret(self):
        nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="none", user=self.user)
        self.assertEqual(nc.credential.auth_mode, OTLPCredential.AuthMode.NONE)
        self.assertEqual(nc.secret, "")
        self.assertIsNone(nc.credential.secret_hash)
        self.assertIsNone(nc.credential.salt)

    def test_rotate_basic(self):
        """Rotation re-issues the secret; the old one stops working."""
        cred = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="basic", user=self.user)
        old = cred.secret
        new = otlp.rotate_credential(cred.credential)
        self.assertNotEqual(new.secret, old)
        self.assertIsNone(otlp.verify_basic(cred.credential.username, old))
        self.assertIsNotNone(otlp.verify_basic(cred.credential.username, new.secret))

    def test_rotate_bearer(self):
        cred = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="bearer", user=self.user)
        old = cred.secret
        new = otlp.rotate_credential(cred.credential)
        self.assertNotEqual(new.secret, old)
        self.assertIsNone(otlp.verify_bearer(old))
        self.assertIsNotNone(otlp.verify_bearer(new.secret))

    def test_rotate_reenables_revoked(self):
        cred = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="basic", user=self.user)
        cred.credential.enabled = False
        cred.credential.save()
        new = otlp.rotate_credential(cred.credential)
        self.assertTrue(new.credential.enabled)
        self.assertIsNotNone(otlp.verify_basic(new.credential.username, new.secret))

    def test_rotate_none_raises(self):
        cred = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="none", user=self.user)
        with self.assertRaises(ValueError):
            otlp.rotate_credential(cred.credential)


class OTLPIngestionEndpointTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="owui-a")
        self.client = Client(SERVER_NAME="localhost")

    def _cred(self, mode):
        return otlp.create_credential(project=self.project, connection=self.conn, auth_mode=mode, user=self.user)

    def test_valid_basic_ingests_and_tags(self):
        nc = self._cred("basic")
        otlp_views.clear_target_spans(nc.credential.target_id)
        resp = self.client.post(
            "/otlp/v1/traces", data=_otlp_body(), content_type="application/json",
            HTTP_AUTHORIZATION=_basic_header(nc.credential.username, nc.secret),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["authenticated"])
        spans = otlp_views.get_spans_for_target(nc.credential.target_id)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["attributes"]["simpleaudit.target_id"], nc.credential.target_id)

    def test_valid_bearer_ingests(self):
        nb = self._cred("bearer")
        resp = self.client.post(
            "/otlp/v1/traces", data=_otlp_body(), content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {nb.secret}",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(otlp_views.get_spans_for_target(nb.credential.target_id)), 1)

    def test_wrong_password_401(self):
        nc = self._cred("basic")
        resp = self.client.post(
            "/otlp/v1/traces", data=_otlp_body(), content_type="application/json",
            HTTP_AUTHORIZATION=_basic_header(nc.credential.username, "wrong"),
        )
        self.assertEqual(resp.status_code, 401)

    def test_no_auth_401(self):
        resp = self.client.post("/otlp/v1/traces", data=_otlp_body(), content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_get_rejected(self):
        self.assertEqual(self.client.get("/otlp/v1/traces").status_code, 405)

    def test_revoke_blocks_subsequent_push(self):
        nc = self._cred("basic")
        header = _basic_header(nc.credential.username, nc.secret)
        self.assertEqual(
            self.client.post("/otlp/v1/traces", data=_otlp_body(), content_type="application/json",
                             HTTP_AUTHORIZATION=header).status_code, 200)
        nc.credential.enabled = False
        nc.credential.save()
        self.assertEqual(
            self.client.post("/otlp/v1/traces", data=_otlp_body(), content_type="application/json",
                             HTTP_AUTHORIZATION=header).status_code, 401)

    def test_malformed_body_ack_not_500(self):
        nc = self._cred("basic")
        resp = self.client.post(
            "/otlp/v1/traces", data="{not json", content_type="application/json",
            HTTP_AUTHORIZATION=_basic_header(nc.credential.username, nc.secret),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["partialSuccess"]["rejectedSpans"], 1)

    def test_none_mode_accepts_unauthenticated(self):
        nc = self._cred("none")
        resp = self.client.post("/otlp/v1/traces", data=_otlp_body(), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["authenticated"])
        spans = otlp_views.get_spans_for_target(nc.credential.target_id)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["attributes"]["simpleaudit.target_id"], nc.credential.target_id)

    def test_none_mode_disabled_blocks_unauthenticated(self):
        nc = self._cred("none")
        nc.credential.enabled = False
        nc.credential.save()
        resp = self.client.post("/otlp/v1/traces", data=_otlp_body(), content_type="application/json")
        self.assertEqual(resp.status_code, 401)


class OtlpSpanPersistenceTest(TestCase):
    """Ingested spans are persisted to OtlpSpan so a separate worker process
    (the auditor) can read what the web process's listener received, by
    (target_id, trace_id)."""

    TRACE_ID = "ef" * 16

    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.conn = ModelConnectionFactory(project=self.project, name="owui-a")
        self.client = Client(SERVER_NAME="localhost")
        self.nc = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="basic", user=self.user)

    def _body(self, spans):
        return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})

    def test_ingest_persists_normalized_span(self):
        body = self._body([{
            "traceId": self.TRACE_ID, "spanId": "11" * 8, "name": "llm chat", "kind": 1,
            "startTimeUnixNano": 1_700_000_000_000_000_000, "endTimeUnixNano": 1_700_000_001_000_000_000,
            "attributes": [{"key": "openinference.span.kind", "value": {"stringValue": "LLM"}}],
            "status": {"code": 0},
        }])
        resp = self.client.post(
            "/otlp/v1/traces", data=body, content_type="application/json",
            HTTP_AUTHORIZATION=_basic_header(self.nc.credential.username, self.nc.secret),
        )
        self.assertEqual(resp.status_code, 200)

        rows = otlp_views.get_spans_for_trace_db(self.nc.credential.target_id, self.TRACE_ID)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["span_id"], "11" * 8)
        self.assertEqual(row["name"], "llm chat")
        self.assertEqual(row["kind"], "LLM")
        # The target tag survives normalization and persistence.
        self.assertEqual(row["attributes"]["simpleaudit.target_id"], self.nc.credential.target_id)

    def test_persist_is_idempotent_on_resend(self):
        """OpenWebUI batches + retries exports; a re-sent span must not duplicate."""
        body = self._body([{"traceId": self.TRACE_ID, "spanId": "22" * 8, "name": "turn", "kind": 1}])
        header = _basic_header(self.nc.credential.username, self.nc.secret)
        for _ in range(3):
            self.assertEqual(
                self.client.post("/otlp/v1/traces", data=body, content_type="application/json",
                                 HTTP_AUTHORIZATION=header).status_code, 200)
        rows = otlp_views.get_spans_for_trace_db(self.nc.credential.target_id, self.TRACE_ID)
        self.assertEqual(len(rows), 1)

    def test_trace_lookup_isolated_per_trace_and_target(self):
        body = self._body([
            {"traceId": self.TRACE_ID, "spanId": "33" * 8, "name": "a", "kind": 1},
            {"traceId": "ab" * 16, "spanId": "44" * 8, "name": "b", "kind": 1},
        ])
        self.client.post(
            "/otlp/v1/traces", data=body, content_type="application/json",
            HTTP_AUTHORIZATION=_basic_header(self.nc.credential.username, self.nc.secret),
        )
        # Per-trace isolation: each trace id returns only its own span.
        self.assertEqual(
            [s["span_id"] for s in otlp_views.get_spans_for_trace_db(self.nc.credential.target_id, self.TRACE_ID)],
            ["33" * 8],
        )
        self.assertEqual(
            [s["span_id"] for s in otlp_views.get_spans_for_trace_db(self.nc.credential.target_id, "ab" * 16)],
            ["44" * 8],
        )
        # Per-target isolation: another target sees none of this target's spans.
        self.assertEqual(otlp_views.get_spans_for_trace_db("other-target", self.TRACE_ID), [])

    def test_persistence_failure_still_acks(self):
        """A DB failure must not fail the export (the target would retry/flood)."""
        with mock.patch("model_registry.otlp_views._persist_spans", side_effect=RuntimeError("db down")):
            resp = self.client.post(
                "/otlp/v1/traces", data=_otlp_body(), content_type="application/json",
                HTTP_AUTHORIZATION=_basic_header(self.nc.credential.username, self.nc.secret),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["authenticated"])
        # The in-memory store still holds the span.
        self.assertEqual(len(otlp_views.get_spans_for_target(self.nc.credential.target_id)), 1)



class OTLPFlagTest(TestCase):
    """The OTLP listener is on by default and can be switched off.

    The routes are wired at URLconf import time based on
    ``model_registry.otlp_config.ENABLED``. When off, the ``/otlp/*`` and
    ``/api/otlp/*`` paths are not registered, so a deployment that doesn't
    want the listener exposes no OTLP surface.
    """

    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client(SERVER_NAME="localhost")

    def _reload_urlconf(self):
        """Re-import config.urls so the otlp routes reflect the current flag."""
        import importlib

        from django.urls import clear_url_caches, set_urlconf

        import config.urls

        importlib.reload(config.urls)
        set_urlconf("config.urls")
        clear_url_caches()

    def _resolvable(self, name):
        """Whether a named route currently resolves (fresh resolver)."""
        from django.urls import NoReverseMatch, reverse

        try:
            reverse(name)
            return True
        except NoReverseMatch:
            return False

    def test_enabled_by_default(self):
        from model_registry import otlp_config

        self.assertTrue(otlp_config.ENABLED)

    def test_routes_present_when_enabled(self):
        from model_registry import otlp_config

        self.assertTrue(otlp_config.ENABLED)
        self._reload_urlconf()
        for name in ("otlp-traces", "otlp-credentials-list", "otlp-credentials-create"):
            self.assertTrue(self._resolvable(name), f"{name} should resolve when enabled")

    def test_routes_absent_when_disabled(self):
        from model_registry import otlp_config

        old = otlp_config.ENABLED
        otlp_config.ENABLED = False
        try:
            self._reload_urlconf()
            for name in ("otlp-traces", "otlp-credentials-list", "otlp-credentials-create"):
                self.assertFalse(self._resolvable(name), f"{name} should not resolve when disabled")
        finally:
            otlp_config.ENABLED = old
            self._reload_urlconf()
