"""Span errors (issue #80) and the scenario queue timeout (issue #82)."""
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase

from infra.ui import _span_error, _trace_span_content_blocks


class SpanErrorTests(SimpleTestCase):
    def test_reads_exception_event_and_status_message(self):
        span = {
            "status": "ERROR", "status_message": "lookup failed", "attributes": {},
            "events": [{"name": "exception", "attributes": {"exception.type": "ValueError",
                                                            "exception.message": "No order D02"}}],
        }
        self.assertEqual(_span_error(span), ("ValueError", "No order D02"))
        self.assertIn(("Error", "ValueError: No order D02"), _trace_span_content_blocks(span))

    def test_status_message_alone(self):
        span = {"status": "ERROR", "status_message": "upstream timeout", "attributes": {"x": 1}}
        self.assertEqual(_span_error(span), ("", "upstream timeout"))
        self.assertIn(("Error", "upstream timeout"), _trace_span_content_blocks(span))

    def test_older_spans_with_copied_attributes_still_work(self):
        span = {"status": "ERROR", "attributes": {"exception.type": "ValueError", "exception.message": "old"}}
        self.assertIn(("Error", "ValueError: old"), _trace_span_content_blocks(span))
        self.assertEqual(_span_error({"attributes": {"error.type": "timeout"}}), ("timeout", ""))
        self.assertEqual(_span_error({"attributes": {}}), ("", ""))


class ScenarioQueueTimeoutTests(SimpleTestCase):
    def test_scenario_task_waits_for_a_slot_instead_of_expiring(self):
        from infra import worker

        client = mock.MagicMock()
        with mock.patch.object(worker, "_scenario_task", None), mock.patch.object(worker, "_finalize_task", None), \
                mock.patch.object(worker, "_tasks_client_id", None):
            worker._register_tasks(client)
        kwargs = next(c.kwargs for c in client.task.call_args_list if c.kwargs.get("name") == "audit.scenario_execute")
        self.assertGreaterEqual(kwargs["schedule_timeout"], timedelta(days=1))
