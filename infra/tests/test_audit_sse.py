"""Tests for the durable SSE progress endpoint.

Verifies that the browser-facing event stream replays from Last-Event-ID and ends
on a terminal run event, using the real Postgres/SQLite AuditEvent rows (no live
Hatchet server required).
"""
from unittest import mock

from django.test import Client, TestCase

from audits.events import append_event
from infra.tests.factories import judge_for


class AuditSSEStreamTest(TestCase):
    def setUp(self):
        from accounts.models import Project, ProjectMembership, User

        self.user = User.objects.create_user(username="sse-user", password="pw12345")
        self.project = Project.objects.create(name="P", slug="p")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client = Client()
        self.client.force_login(self.user)

    def _events_url(self, run_id):
        return f"/api/projects/{self.project.id}/audit-runs/{run_id}/events/"

    def _make_run(self):
        from audits.models import AuditRun
        from model_registry.models import ModelConnection, RegisteredModel
        from scenarios.models import (
            Scenario,
            ScenarioRevision,
            ScenarioSet,
            ScenarioSetVersion,
        )

        # Build minimal immutable inputs directly; SSE only reads events + status.
        conn = ModelConnection.objects.create(
            project=self.project, name="c", provider="openai", base_url="http://x"
        )
        target = RegisteredModel.objects.create(connection=conn, project=self.project, display_name="t", model_id="m")
        # A run needs a scenario_set_version FK; create the minimal chain.
        scenario = Scenario.objects.create(project=self.project, key="k", title="T")
        revision = ScenarioRevision.objects.create(
            scenario=scenario, revision=1, description="d", content_hash="h"
        )
        sset = ScenarioSet.objects.create(project=self.project, name="S")
        version = ScenarioSetVersion.objects.create(
            scenario_set=sset, version=1, content_hash="vh", scenario_count=1
        )
        from scenarios.models import ScenarioSetVersionItem

        ScenarioSetVersionItem.objects.create(version=version, scenario=scenario, revision=revision, position=1)

        run = AuditRun.objects.create(
            project=self.project,
            name="run",
            status=AuditRun.Status.QUEUED,
            scenario_set_version=version,
            target_model=target,
            auditor_model=target,
            judge_model=target, judge_version=judge_for(target),
            target_config_snapshot={},
            auditor_config_snapshot={},
            judge_config_snapshot={},
            generation_parameters_snapshot={},
            simpleaudit_version="0.1.0",
            git_commit="abc123",
            total_scenarios=1,
            created_by=self.user,
        )
        return run

    def test_stream_replays_from_last_event_id_and_ends_on_terminal(self):
        run = self._make_run()
        e1 = append_event(run.id, "a", "scenario_attempted", {"attempt": 1})
        e2 = append_event(run.id, "a", "scenario_completed", {"attempt": 1})
        e3 = append_event(run.id, "_run", "run_completed", {"scenarios": 1})

        # Reconnect with Last-Event-ID == e1 -> should receive e2 and e3, then end.
        response = self.client.get(self._events_url(run.id), HTTP_LAST_EVENT_ID=str(e1))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")

        body = b"".join(response.streaming_content).decode()
        self.assertIn(f"id: {e2}", body)
        self.assertIn(f"id: {e3}", body)
        self.assertNotIn(f"id: {e1}", body)
        self.assertIn("event: run_completed", body)

    def test_stream_requires_project_access(self):
        run = self._make_run()
        from accounts.models import User

        outsider = User.objects.create_user(username="outsider", password="pw12345")
        client2 = Client()
        client2.force_login(outsider)
        response = client2.get(self._events_url(run.id))
        self.assertEqual(response.status_code, 403)

    def _build_request(self, run_id, after_id='0'):
        """Build an authenticated GET request for the SSE view."""
        from django.test import RequestFactory

        rf = RequestFactory()
        request = rf.get(f'/api/projects/{self.project.id}/audit-runs/{run_id}/events/')
        request.META['HTTP_LAST_EVENT_ID'] = after_id
        request.user = self.user
        return request

    def _get_raw_generator(self, run_id, after_id='0'):
        """Call the view and extract the raw event_stream generator.

        StreamingHttpResponse wraps our generator in a map(str.encode, ...).
        We intercept the constructor to capture the original iterable before
        wrapping, giving us direct close()/next() access for deterministic tests.
        """
        from django.http import StreamingHttpResponse

        from audits.views import stream_audit_run_events

        captured = {}

        original_init = StreamingHttpResponse.__init__

        def patched_init(self, *args, **kwargs):
            if args:
                captured['iterable'] = args[0]
            elif 'streaming_content' in kwargs:
                captured['iterable'] = kwargs['streaming_content']
            original_init(self, *args, **kwargs)

        request = self._build_request(run_id, after_id)
        with mock.patch.object(StreamingHttpResponse, '__init__', patched_init):
            stream_audit_run_events(request, self.project.id, run_id)

        gen = captured.get('iterable')
        assert gen is not None, 'Failed to capture the streaming iterable'
        return gen

    def test_stream_survives_generator_exit_without_error(self):
        """A client disconnect (GeneratorExit) mid-stream must not raise.

        Regression test for Sentry issue #25: the long-running SSE poll loop
        used to surface SystemExit / GeneratorExit as unhandled errors. The
        generator must catch and re-raise them cleanly so Python's normal
        close semantics apply without propagating an error to the caller.
        """
        run = self._make_run()
        # No terminal event -> the stream would keep polling forever if left alone.
        append_event(run.id, 'a', 'scenario_attempted', {'attempt': 1})

        gen = self._get_raw_generator(run.id)

        # Make the poll sleep a no-op so the loop runs fast.
        with mock.patch('audits.views.time.sleep'):
            # Consume the first chunk (the non-terminal scenario_attempted event).
            first = next(gen)
            self.assertIn('event: scenario_attempted', first)

            # Simulate the server closing the response iterator mid-loop — this
            # is exactly what Django does when the client disconnects. Must not
            # raise anything other than GeneratorExit (which is swallowed by
            # the except block in event_stream).
            gen.close()
            # After close(), the generator is exhausted; next() raises StopIteration.
            with self.assertRaises(StopIteration):
                next(gen)

    def test_stream_survives_system_exit_during_sleep(self):
        """SystemExit raised inside the poll sleep must be caught and re-raised.

        This is the exact traceback from Sentry issue #25: a worker process
        receiving SIGTERM while blocked in time.sleep(). The generator catches
        SystemExit, logs at debug level, and re-raises so Python can unwind
        cleanly instead of surfacing it as an application error.
        """
        run = self._make_run()
        # A single non-terminal event so the loop reaches its sleep branch.
        append_event(run.id, 'a', 'scenario_attempted', {'attempt': 1})

        gen = self._get_raw_generator(run.id)

        # Let the first iteration emit the event and enter the sleep branch.
        first = next(gen)
        self.assertIn('event: scenario_attempted', first)

        # On the second call into the generator, time.sleep raises SystemExit(1)
        # — simulating the OS sending SIGTERM to the worker process.
        def fake_sleep(_seconds):
            raise SystemExit(1)

        with mock.patch('audits.views.time.sleep', side_effect=fake_sleep), \
             self.assertRaises(SystemExit) as ctx:
            next(gen)
        self.assertEqual(ctx.exception.code, 1)
