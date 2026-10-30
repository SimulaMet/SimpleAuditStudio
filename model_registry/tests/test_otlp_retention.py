"""Span retention: unclaimed spans are held briefly, claimed traces are kept, stale ones pruned."""
import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.utils import timezone
from simpleaudit.tracing.context import TraceCorrelation

from infra import tracing
from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    UserFactory,
)
from model_registry import otlp_services as otlp
from model_registry import otlp_views
from model_registry.models import OtlpSpan, OtlpTraceClaim
from model_registry.tests.test_otlp_receiver import _basic_header

TRACE_A = "aa" * 16
TRACE_B = "bb" * 16


def _span(trace_id, span_id):
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "lookup_order",
        "attributes": [{"key": "gen_ai.tool.name", "value": {"stringValue": "acme_get_order"}}],
    }


def _export(spans):
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})


class SpanRetentionTest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        conn = ModelConnectionFactory(project=self.project, name="owui-retention")
        self.client = Client(SERVER_NAME="localhost")
        self.basic = otlp.create_credential(
            project=self.project, connection=conn, auth_mode="basic", user=self.user,
        )
        self.target_id = self.basic.credential.target_id
        otlp_views.clear_target_spans(self.target_id)

    def _post(self, spans):
        return self.client.post(
            "/otlp/v1/traces",
            data=_export(spans),
            content_type="application/json",
            HTTP_AUTHORIZATION=_basic_header(self.basic.credential.username, self.basic.secret),
        )

    def _held(self, trace_id):
        return OtlpSpan.objects.filter(target_id=self.target_id, trace_id=trace_id, held_until__isnull=False)

    def _kept(self, trace_id):
        return OtlpSpan.objects.filter(target_id=self.target_id, trace_id=trace_id, held_until__isnull=True)

    def test_unclaimed_spans_are_held(self):
        self._post([_span(TRACE_A, "01" * 8)])
        self.assertEqual(self._held(TRACE_A).count(), 1)
        self.assertEqual(self._kept(TRACE_A).count(), 0)

    def test_claim_promotes_held_spans_and_keeps_later_arrivals(self):
        self._post([_span(TRACE_A, "01" * 8)])
        otlp_views.get_spans_for_trace_db(self.target_id, TRACE_A)  # the run's evidence fetch
        self.assertEqual(self._kept(TRACE_A).count(), 1)
        self._post([_span(TRACE_A, "02" * 8)])  # late batch for a claimed trace
        self.assertEqual(self._kept(TRACE_A).count(), 2)
        self.assertEqual(self._held(TRACE_A).count(), 0)

    def test_repeated_export_does_not_re_hold_a_claimed_span(self):
        self._post([_span(TRACE_A, "01" * 8)])
        otlp_views.get_spans_for_trace_db(self.target_id, TRACE_A)
        self._post([_span(TRACE_A, "01" * 8)])
        self.assertEqual(self._kept(TRACE_A).count(), 1)

    def test_span_without_trace_id_is_not_stored(self):
        self._post([_span("", "01" * 8)])
        self.assertFalse(OtlpSpan.objects.filter(target_id=self.target_id).exists())

    def test_expired_holds_are_pruned_but_claimed_spans_survive(self):
        self._post([_span(TRACE_A, "01" * 8), _span(TRACE_B, "02" * 8)])
        otlp_views.get_spans_for_trace_db(self.target_id, TRACE_A)
        later = timezone.now() + otlp.SPAN_HOLD_TTL + timedelta(minutes=1)
        otlp.prune_spans(now=later)
        self.assertEqual(self._kept(TRACE_A).count(), 1)
        self.assertFalse(OtlpSpan.objects.filter(trace_id=TRACE_B).exists())

    def test_held_spans_are_capped_per_target_newest_first(self):
        self._post([_span(TRACE_B, f"{i:02x}" * 8) for i in range(5)])
        with mock.patch.object(otlp, "SPAN_HOLD_CAP", 3):
            otlp.prune_spans()
        self.assertEqual(self._held(TRACE_B).count(), 3)

    def test_stale_claim_releases_its_kept_spans(self):
        self._post([_span(TRACE_A, "01" * 8)])
        otlp_views.get_spans_for_trace_db(self.target_id, TRACE_A)
        much_later = timezone.now() + otlp.CLAIMED_SPAN_RETENTION + timedelta(days=1)
        otlp.prune_spans(now=much_later)
        self.assertFalse(OtlpSpan.objects.filter(trace_id=TRACE_A).exists())
        self.assertFalse(OtlpTraceClaim.objects.filter(trace_id=TRACE_A).exists())

    def test_in_memory_store_is_capped(self):
        with mock.patch.object(otlp_views, "IN_MEMORY_SPAN_CAP", 2):
            self._post([_span(TRACE_A, f"{i:02x}" * 8) for i in range(4)])
        self.assertEqual(len(otlp_views._SPAN_STORE[self.target_id]), 2)


class SpanWriteBufferTest(TestCase):
    """Exports queue in memory; the DB sees them at flush or on a read."""

    setUp_base = SpanRetentionTest.setUp
    _post = SpanRetentionTest._post
    _held = SpanRetentionTest._held
    _kept = SpanRetentionTest._kept

    def setUp(self):
        self.setUp_base()
        # Set after the autouse fixture (which forces 0), so this window wins.
        flush_window = override_settings(OTLP_SPAN_FLUSH_INTERVAL_S=60)
        flush_window.enable()
        self.addCleanup(flush_window.disable)

    def tearDown(self):
        otlp_views.flush_pending()  # drop what this test buffered
        if otlp_views._flush_timer is not None:
            otlp_views._flush_timer.cancel()
            otlp_views._flush_timer = None
        super().tearDown()

    def test_export_is_buffered_until_flush(self):
        self._post([_span(TRACE_A, "01" * 8)])
        self.assertFalse(OtlpSpan.objects.filter(trace_id=TRACE_A).exists())
        otlp_views.flush_pending(self.target_id)
        self.assertEqual(self._held(TRACE_A).count(), 1)

    def test_read_flushes_this_targets_buffer_first(self):
        self._post([_span(TRACE_A, "01" * 8)])
        rows = otlp_views.get_spans_for_trace_db(self.target_id, TRACE_A)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self._kept(TRACE_A).count(), 1)

    def test_repeated_exports_in_one_window_write_once(self):
        self._post([_span(TRACE_A, "01" * 8)])
        self._post([_span(TRACE_A, "01" * 8), _span(TRACE_A, "02" * 8)])
        otlp_views.flush_pending(self.target_id)
        self.assertEqual(OtlpSpan.objects.filter(trace_id=TRACE_A).count(), 2)


class PerTurnClaimTest(TestCase):
    """A turn's trace id is claimed as the run records it, not at the end of the run."""

    setUp_base = SpanRetentionTest.setUp
    _post = SpanRetentionTest._post
    _held = SpanRetentionTest._held
    _kept = SpanRetentionTest._kept

    def setUp(self):
        self.setUp_base()
        self._inline = _InlinePool()
        patcher = mock.patch.object(tracing, "_SPAN_CLAIM_POOL", self._inline)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claim_keeps_held_spans_of_that_turn(self):
        self._post([_span(TRACE_A, "01" * 8)])
        tracing._claim_trace(self.target_id, TRACE_A)
        self.assertEqual(self._kept(TRACE_A).count(), 1)

    def test_recording_a_turn_claims_its_trace(self):
        self._post([_span(TRACE_A, "01" * 8)])
        provider = tracing.StudioTraceProvider(self.target_id)
        correlation = TraceCorrelation(audit_run_id="audit_t", on_new_trace=provider.claim)
        correlation.record("turn_1", TRACE_A)
        self.assertEqual(self._kept(TRACE_A).count(), 1)

    def test_other_traces_stay_held_until_their_turn_claims(self):
        self._post([_span(TRACE_A, "01" * 8), _span(TRACE_B, "02" * 8)])
        tracing._claim_trace(self.target_id, TRACE_A)
        self.assertEqual(self._held(TRACE_B).count(), 1)


class _InlinePool:
    """Stands in for the claim pool: runs submitted work immediately."""

    def submit(self, fn, *args):
        fn(*args)


class BufferBackpressureTest(TestCase):
    """A full write buffer answers 429 and keeps nothing from that export."""

    setUp_base = SpanRetentionTest.setUp
    _post = SpanRetentionTest._post

    def setUp(self):
        self.setUp_base()
        flush_window = override_settings(OTLP_SPAN_FLUSH_INTERVAL_S=60)
        flush_window.enable()
        self.addCleanup(flush_window.disable)
        cap = mock.patch.object(otlp_views, "MAX_PENDING_SPANS", 5)
        cap.start()
        self.addCleanup(cap.stop)

    def tearDown(self):
        otlp_views.flush_pending()
        if otlp_views._flush_timer is not None:
            otlp_views._flush_timer.cancel()
            otlp_views._flush_timer = None
        super().tearDown()

    def test_export_that_does_not_fit_gets_429_and_nothing_is_kept(self):
        self.assertEqual(self._post([_span(TRACE_A, f"{i:02x}" * 8) for i in range(3)]).status_code, 200)
        resp = self._post([_span(TRACE_B, f"{i:02x}" * 8) for i in range(4)])
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp["Retry-After"], "1")
        otlp_views.flush_pending()
        self.assertEqual(OtlpSpan.objects.filter(trace_id=TRACE_B).count(), 0)
        self.assertEqual(OtlpSpan.objects.filter(trace_id=TRACE_A).count(), 3)

    def test_export_that_fits_is_accepted(self):
        self.assertEqual(self._post([_span(TRACE_A, f"{i:02x}" * 8) for i in range(5)]).status_code, 200)
