"""Monitors: tick, drift statistics and monitor pages.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_monitors
"""
from datetime import UTC, timedelta
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from audits.models import AuditRun, Monitor
from audits.monitors import (
    advance,
    drift_series,
    run_due_monitors,
    two_proportion_z,
    wilson,
)
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    ScenarioResultFactory,
    ScenarioSetFactory,
    ScenarioSetVersionFactory,
    UserFactory,
)

_PROVENANCE = mock.Mock(version="0.2.1", commit="", source="metadata")


def _result(severities):
    """ScenarioResult payload with one trial per severity."""
    if len(severities) == 1:
        return {"severity": severities[0]}
    return {"reps": [{"severity": s} for s in severities], "n_repetitions": len(severities)}


class MonitorTestBase(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.sset = ScenarioSetFactory(project=self.project)
        self.v1 = ScenarioSetVersionFactory(scenario_set=self.sset, version=1)
        self.conn = ModelConnectionFactory(project=self.project)
        self.model = RegisteredModelFactory(connection=self.conn)
        self.now = timezone.now()

        patcher = mock.patch("audits.services.resolve_engine_provenance", return_value=_PROVENANCE)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No Hatchet in tests: submission is exercised separately.
        for target in ("audits.services.submit_audit_run", "infra.ui.submit_audit_run"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _monitor(self, **kw):
        defaults = {
            "project": self.project,
            "name": "Weekly",
            "scenario_set": self.sset,
            "scenario_set_version": self.v1,
            "target_model": self.model,
            "auditor_model": self.model,
            "judge_model": self.model,
            "generation_parameters": {"max_turns": 3, "n_repetitions": 2, "judge_params": {"temperature": 0}},
            "interval_hours": 168,
            "next_run_at": self.now - timedelta(minutes=1),
            "created_by": self.user,
        }
        defaults.update(kw)
        return Monitor.objects.create(**defaults)


class AdvanceTests(TestCase):
    def test_future_tick_unchanged(self):
        now = timezone.now()
        nxt = now + timedelta(hours=1)
        self.assertEqual(advance(nxt, 24, now), nxt)

    def test_missed_ticks_are_skipped_not_replayed(self):
        now = timezone.now()
        stale = now - timedelta(hours=24 * 3 + 1)
        nxt = advance(stale, 24, now)
        self.assertGreater(nxt, now)
        self.assertLessEqual(nxt - now, timedelta(hours=24))
        # Stays on the original grid.
        self.assertEqual((nxt - stale) % timedelta(hours=24), timedelta(0))


class TickTests(MonitorTestBase):
    def test_due_monitor_launches_frozen_run_and_advances(self):
        s = self._monitor()
        ids = run_due_monitors(now=self.now)
        self.assertEqual(len(ids), 1)
        run = AuditRun.objects.get(pk=ids[0])
        self.assertEqual(run.monitor_id, s.id)
        self.assertEqual(run.scenario_set_version_id, self.v1.id)
        self.assertEqual(
            run.generation_parameters_snapshot,
            {"max_turns": 3, "n_repetitions": 2, "judge_params": {"temperature": 0}},
        )
        s.refresh_from_db()
        self.assertEqual(s.last_run_id, run.id)
        self.assertGreater(s.next_run_at, self.now)
        self.assertEqual(s.last_error, "")

    def test_not_due_and_disabled_are_ignored(self):
        self._monitor(next_run_at=self.now + timedelta(hours=1))
        self._monitor(name="Paused", enabled=False)
        self.assertEqual(run_due_monitors(now=self.now), [])

    def test_skips_while_previous_run_in_flight(self):
        s = self._monitor()
        active = AuditRunFactory(project=self.project, status=AuditRun.Status.JUDGING, monitor=s)
        s.last_run = active
        s.save()
        self.assertEqual(run_due_monitors(now=self.now), [])
        s.refresh_from_db()
        self.assertIn("still judging", s.last_error)
        self.assertGreater(s.next_run_at, self.now)

    def test_unpinned_follows_latest_version(self):
        v2 = ScenarioSetVersionFactory(scenario_set=self.sset, version=2)
        self._monitor(scenario_set_version=None)
        run = AuditRun.objects.get(pk=run_due_monitors(now=self.now)[0])
        self.assertEqual(run.scenario_set_version_id, v2.id)

    def test_failure_is_recorded_and_still_advances(self):
        empty_set = ScenarioSetFactory(project=self.project)
        s = self._monitor(scenario_set=empty_set, scenario_set_version=None)
        self.assertEqual(run_due_monitors(now=self.now), [])
        s.refresh_from_db()
        self.assertIn("no published version", s.last_error)
        self.assertTrue(s.enabled)
        self.assertGreater(s.next_run_at, self.now)

    def test_deleted_owner_pauses_monitor(self):
        s = self._monitor(created_by=None)
        self.assertEqual(run_due_monitors(now=self.now), [])
        s.refresh_from_db()
        self.assertFalse(s.enabled)
        self.assertIn("deleted user", s.last_error)

    def test_submits_created_runs(self):
        self._monitor()
        with mock.patch("audits.services.submit_audit_run") as submit:
            ids = run_due_monitors(now=self.now)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0].pk, ids[0])


class StatsTests(MonitorTestBase):
    def test_wilson_bounds(self):
        lo, hi = wilson(50, 100)
        self.assertAlmostEqual(lo, 0.404, places=2)
        self.assertAlmostEqual(hi, 0.596, places=2)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))

    def test_two_proportion_z_sign(self):
        self.assertLess(two_proportion_z(90, 100, 60, 100), -1.96)
        self.assertIsNone(two_proportion_z(0, 0, 1, 2))

    def _run(self, s, severities_per_scenario, version=None):
        run = AuditRunFactory(project=self.project, monitor=s, scenario_set_version=version or self.v1)
        for i, sev in enumerate(severities_per_scenario):
            ScenarioResultFactory(run_id=run.pk, version_item_id=str(i), result=_result(sev))
        return run

    def test_drift_flags_significant_drop_only(self):
        s = self._monitor()
        passing = [["pass"] * 5] * 10  # 50/50 trials pass
        for _ in range(3):
            self._run(s, passing)
        self._run(s, [["pass"] * 5] * 9 + [["pass"] * 4 + ["high"]])  # 49/50: noise
        self._run(s, [["pass", "high", "high", "high", "high"]] * 10)  # 10/50: drop
        points = drift_series(s)
        self.assertEqual([p["change"] for p in points], ["", "", "", "", "drop"])
        self.assertEqual(points[0]["n"], 50)

    def test_errors_excluded_from_trials(self):
        s = self._monitor()
        self._run(s, [["pass", "ERROR"], ["high"]])
        p = drift_series(s)[0]
        self.assertEqual((p["k"], p["n"], p["errors"]), (1, 2, 1))

    def test_version_change_resets_baseline(self):
        s = self._monitor()
        v2 = ScenarioSetVersionFactory(scenario_set=self.sset, version=2)
        self._run(s, [["pass"] * 5] * 10)
        self._run(s, [["high"] * 5] * 10, version=v2)
        points = drift_series(s)
        self.assertTrue(points[1]["version_changed"])
        self.assertEqual(points[1]["change"], "")


class _ClientMixin:
    def _member(self, role):
        user = UserFactory()
        user.set_password("pw")
        user.save()
        MembershipFactory(user=user, project=self.project, role=role)
        client = Client()
        client.login(username=user.username, password="pw")
        return user, client

    def _design(self, **extra):
        """A one-run New Experiment design with Repeat set (weekly, start now)."""
        data = {
            "scenario_set": [self.sset.id],
            "target_model": [self.model.id],
            "auditor_model": [self.model.id],
            "judge_model": [self.model.id],
            "max_turns": "3",
            "n_repetitions": "2",
            "gen_config_json": '{"judge_params": {"temperature": 0}}',
            "repeat": "168",
            "timezone": "UTC",
            "start": "now",
        }
        data.update(extra)
        return data


class MonitorPagesTests(_ClientMixin, MonitorTestBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")

    def test_list_is_list_only_with_new_monitor_link(self):
        self._monitor()
        page = self.client.get("/monitors/")
        self.assertContains(page, "/experiments/new/?repeat=168")
        self.assertNotContains(page, 'name="cron_expression"')
        self.assertEqual(self.client.post("/monitors/", {}).status_code, 405)

    def test_detail_renders_chart(self):
        m = self._monitor()
        run = AuditRunFactory(project=self.project, monitor=m, scenario_set_version=self.v1)
        ScenarioResultFactory(run_id=run.pk, result=_result(["pass", "high"]))
        resp = self.client.get(f"/monitors/{m.id}/")
        self.assertContains(resp, "<polyline")

    def test_actions(self):
        m = self._monitor()
        self.client.post(f"/monitors/{m.id}/toggle/")
        m.refresh_from_db()
        self.assertFalse(m.enabled)
        resp = self.client.post(f"/monitors/{m.id}/run-now/")
        run = AuditRun.objects.get(monitor=m)
        self.assertRedirects(resp, f"/runs/{run.id}/", fetch_redirect_response=False)
        self.client.post(f"/monitors/{m.id}/delete/")
        self.assertFalse(Monitor.objects.filter(pk=m.id).exists())
        run.refresh_from_db()
        self.assertIsNone(run.monitor_id)  # runs outlive their monitor

    def test_other_project_monitor_is_404(self):
        other_set = ScenarioSetFactory()
        other_model = RegisteredModelFactory(project=other_set.project)
        m = self._monitor(
            project=other_set.project, scenario_set=other_set, scenario_set_version=None,
            target_model=other_model, auditor_model=other_model, judge_model=other_model,
        )
        self.assertEqual(self.client.get(f"/monitors/{m.id}/").status_code, 404)
        self.assertEqual(self.client.post(f"/monitors/{m.id}/delete/").status_code, 404)


class MonitorPermissionTests(_ClientMixin, MonitorTestBase):
    """View: any member. Create: admin/auditor. Manage: admin or creating auditor."""

    def test_viewer_can_view_but_not_create_or_manage(self):
        m = self._monitor()
        _, client = self._member("viewer")
        self.assertEqual(client.get("/monitors/").status_code, 200)
        self.assertEqual(client.get(f"/monitors/{m.id}/").status_code, 200)
        # Even a later start (no run created now) must not let a viewer make a monitor.
        resp = client.post("/experiments/new/", self._design(start="at", first_run_at="2099-01-01T00:00"))
        self.assertContains(resp, "Insufficient project role")
        for action in ("toggle", "run-now", "delete"):
            self.assertEqual(client.post(f"/monitors/{m.id}/{action}/").status_code, 403)
        self.assertEqual(Monitor.objects.count(), 1)

    def test_auditor_creates_and_manages_own_but_not_others(self):
        others = self._monitor()  # created by the admin
        auditor, client = self._member("auditor")
        client.post("/experiments/new/", self._design())
        mine = Monitor.objects.exclude(pk=others.pk).get()
        self.assertEqual(mine.created_by, auditor)
        self.assertEqual(client.post(f"/monitors/{mine.id}/toggle/").status_code, 302)
        self.assertEqual(client.post(f"/monitors/{others.id}/toggle/").status_code, 403)
        self.assertEqual(client.post(f"/monitors/{others.id}/delete/").status_code, 403)

    def test_admin_manages_any_monitor(self):
        auditor, _ = self._member("auditor")
        m = self._monitor(created_by=auditor)
        _, client = self._member("admin")
        self.assertEqual(client.post(f"/monitors/{m.id}/delete/").status_code, 302)
        self.assertFalse(Monitor.objects.filter(pk=m.id).exists())

    def test_interval_minimum_and_cap(self):
        from audits.monitors import MAX_MONITORS_PER_PROJECT

        _, client = self._member("admin")
        resp = client.post("/experiments/new/", self._design(repeat="1"))
        self.assertContains(resp, "between 6 hours")
        for i in range(MAX_MONITORS_PER_PROJECT):
            self._monitor(name=f"S{i}")
        resp = client.post("/experiments/new/", self._design())
        self.assertContains(resp, "limit")
        self.assertEqual(Monitor.objects.count(), MAX_MONITORS_PER_PROJECT)
        self.assertFalse(AuditRun.objects.exists())  # all or nothing

    def test_tick_pauses_monitor_when_owner_downgraded(self):
        auditor, _ = self._member("auditor")
        m = self._monitor(created_by=auditor)
        auditor.memberships.filter(project=self.project).update(role="viewer")
        self.assertEqual(run_due_monitors(now=self.now), [])
        m.refresh_from_db()
        self.assertFalse(m.enabled)
        self.assertIn("no longer has admin or auditor role", m.last_error)

    def test_cannot_resume_monitor_of_downgraded_owner(self):
        auditor, _ = self._member("auditor")
        m = self._monitor(created_by=auditor, enabled=False)
        auditor.memberships.filter(project=self.project).update(role="viewer")
        _, admin_client = self._member("admin")
        admin_client.post(f"/monitors/{m.id}/toggle/")
        m.refresh_from_db()
        self.assertFalse(m.enabled)


class LaunchPermissionTests(MonitorTestBase):
    def test_viewer_cannot_launch_one_off_run(self):
        viewer = UserFactory()
        viewer.set_password("pw")
        viewer.save()
        MembershipFactory(user=viewer, project=self.project, role="viewer")
        client = Client()
        client.login(username=viewer.username, password="pw")
        page = client.get("/experiments/new/")
        self.assertContains(page, "view-only access")
        resp = client.post("/experiments/new/", {
            "scenario_set": self.sset.id,
            "target_model": self.model.id,
            "auditor_model": self.model.id,
            "judge_model": self.model.id,
        })
        self.assertContains(resp, "Insufficient project role")
        self.assertFalse(AuditRun.objects.exists())


class CronMonitorTests(_ClientMixin, MonitorTestBase):
    def test_validate_cron_accepts_and_normalises(self):
        from audits.monitors import validate_cron

        self.assertEqual(validate_cron("  0   6 * *  1 "), "0 6 * * 1")
        # No minimum frequency for cron monitors.
        self.assertEqual(validate_cron("* * * * *"), "* * * * *")
        self.assertEqual(validate_cron("0 0,1 * * *"), "0 0,1 * * *")

    def test_validate_cron_rejects_bad_syntax(self):
        from audits.monitors import validate_cron

        for expr, msg in [("61 * * * *", "Invalid cron"), ("0 6 * *", "5 fields"), ("0 0 30 2 *", "Invalid cron")]:
            with self.subTest(expr=expr), self.assertRaisesMessage(ValueError, msg):
                validate_cron(expr)

    def test_tick_moves_to_next_cron_firing(self):
        from datetime import datetime

        now = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)  # a Sunday
        m = self._monitor(cron_expression="0 6 * * 1", next_run_at=now - timedelta(days=8))
        self.assertEqual(len(run_due_monitors(now=now)), 1)
        m.refresh_from_db()
        self.assertEqual(m.next_run_at, datetime(2026, 9, 28, 6, 0, tzinfo=UTC))

    def test_repeat_with_cron_in_timezone(self):
        self.client.login(username=self.user.username, password="pw")
        self.client.post("/experiments/new/", self._design(
            repeat="cron", cron_expression="0 6 * * *", timezone="Europe/Oslo",
            start="at", first_run_at="2030-01-10T12:00",
        ))
        m = Monitor.objects.get()
        # 06:00 Oslo on Jan 11 (CET, UTC+1) = 05:00 UTC; nothing ran now.
        self.assertEqual(m.next_run_at.isoformat(), "2030-01-11T05:00:00+00:00")
        self.assertIn("(Europe/Oslo)", m.interval_display)
        self.assertFalse(AuditRun.objects.exists())

    def test_bad_cron_and_timezone_are_explained(self):
        self.client.login(username=self.user.username, password="pw")
        resp = self.client.post("/experiments/new/", self._design(repeat="cron", cron_expression="0 25 * * *"))
        self.assertContains(resp, "Invalid cron")
        resp = self.client.post("/experiments/new/", self._design(timezone="Mars/Olympus"))
        self.assertContains(resp, "Unknown timezone")
        self.assertFalse(Monitor.objects.exists())

    def test_cron_follows_daylight_saving(self):
        from datetime import datetime

        from audits.monitors import cron_next

        # Oslo leaves summer time on 2026-10-25: 06:00 local moves from 04:00 to 05:00 UTC.
        before = cron_next("0 6 * * *", datetime(2026, 10, 23, 12, tzinfo=UTC), "Europe/Oslo")
        after = cron_next("0 6 * * *", datetime(2026, 10, 25, 12, tzinfo=UTC), "Europe/Oslo")
        self.assertEqual(before.isoformat(), "2026-10-24T04:00:00+00:00")
        self.assertEqual(after.isoformat(), "2026-10-26T05:00:00+00:00")


class RepeatFlowTests(_ClientMixin, MonitorTestBase):
    """Repeat on New Experiment creates monitors; the old /monitors/ form is gone."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")

    def test_once_creates_no_monitor(self):
        self.client.post("/experiments/new/", self._design(repeat="once"))
        self.assertEqual(AuditRun.objects.count(), 1)
        self.assertFalse(Monitor.objects.exists())

    def test_start_now_runs_and_monitors_with_first_point(self):
        resp = self.client.post("/experiments/new/", self._design())
        run = AuditRun.objects.get()
        m = Monitor.objects.get()
        self.assertRedirects(resp, f"/runs/{run.id}/", fetch_redirect_response=False)
        self.assertEqual(run.monitor_id, m.id)
        self.assertEqual(m.last_run_id, run.id)
        self.assertEqual(m.scenario_set_version_id, self.v1.id)  # always pinned
        self.assertEqual(m.interval_hours, 168)
        self.assertGreater(m.next_run_at, timezone.now() + timedelta(days=6))
        self.assertEqual(m.generation_parameters, run.generation_parameters_snapshot)

    def test_start_at_creates_monitor_only(self):
        resp = self.client.post("/experiments/new/", self._design(start="at", first_run_at="2099-03-01T09:00"))
        m = Monitor.objects.get()
        self.assertRedirects(resp, f"/monitors/{m.id}/", fetch_redirect_response=False)
        self.assertFalse(AuditRun.objects.exists())
        self.assertEqual(m.next_run_at.isoformat(), "2099-03-01T09:00:00+00:00")

    def test_start_in_the_past_rejected(self):
        resp = self.client.post("/experiments/new/", self._design(start="at", first_run_at="2001-01-01T00:00"))
        self.assertContains(resp, "in the past")
        self.assertFalse(Monitor.objects.exists())

    def _existing_run(self):
        return AuditRunFactory(
            project=self.project, scenario_set_version=self.v1,
            target_model=self.model, auditor_model=self.model, judge_model=self.model,
            generation_parameters_snapshot={"max_turns": 3, "n_repetitions": 2, "judge_params": {"temperature": 0}},
        )

    def test_monitor_for_drift_link_and_prefill(self):
        run = self._existing_run()
        page = self.client.get(f"/runs/{run.id}/")
        self.assertContains(page, f"/experiments/new/?clone_from={run.id}&repeat=168")
        design = self.client.get(f"/experiments/new/?clone_from={run.id}&repeat=168")
        self.assertEqual(design.context["rep"]["repeat"], "168")
        self.assertEqual(design.context["rep"]["start"], "baseline")
        self.assertContains(design, f"Use run #{run.id} as the first point")

    def test_baseline_start_attaches_existing_run(self):
        run = self._existing_run()
        self.client.post("/experiments/new/", self._design(
            start="baseline", clone_from=str(run.id), scenario_set_version=str(self.v1.id)
        ))
        m = Monitor.objects.get()
        run.refresh_from_db()
        self.assertEqual(run.monitor_id, m.id)
        self.assertEqual(AuditRun.objects.count(), 1)  # nothing new ran

    def test_baseline_with_changed_settings_not_attached(self):
        run = self._existing_run()
        self.client.post("/experiments/new/", self._design(start="baseline", clone_from=str(run.id), max_turns="7"))
        run.refresh_from_db()
        self.assertIsNone(run.monitor_id)
        self.assertEqual(Monitor.objects.count(), 1)

    def test_experiment_repeat_creates_linked_monitors(self):
        second = RegisteredModelFactory(connection=self.conn, display_name="Other")
        review = self.client.post("/experiments/new/", self._design(target_model=[self.model.id, second.id]))
        self.assertTemplateUsed(review, "experiment_review.html")
        self.assertContains(review, "Repeats weekly")
        payload = {"action": "launch", "experiment_name": "Weekly targets", "row_count": review.context["row_count"]}
        for key, value in review.context["design_post"]:
            payload.setdefault(f"design__{key}", []).append(value)
        for row in review.context["rows"]:
            i = row["i"]
            payload.update({
                f"run-{i}-spec": row["spec"], f"run-{i}-name": row["name"], f"run-{i}-include": "1",
                f"run-{i}-max_turns": row["max_turns"], f"run-{i}-language": row["language"],
                f"run-{i}-n_repetitions": row["n_repetitions"], f"run-{i}-gen_config_json": row["gen_config_json"],
            })
        self.client.post("/experiments/new/", payload)
        from audits.models import Experiment

        exp = Experiment.objects.get()
        monitors = list(exp.monitors.all())
        self.assertEqual(len(monitors), 2)
        self.assertEqual({r.monitor_id for r in exp.runs.all()}, {m.id for m in monitors})
        # A later tick adds a run to the same experiment.
        Monitor.objects.update(next_run_at=self.now - timedelta(minutes=1))
        AuditRun.objects.update(status="completed")
        run_due_monitors(now=self.now)
        self.assertEqual(exp.runs.count(), 4)
        page = self.client.get(f"/experiments/{exp.id}/")
        self.assertContains(page, "Over time")
        self.assertContains(page, "Latest run per setup")
        # Latest per setup: 2 current runs; pooled: all 4.
        self.assertEqual(len(page.context["current_runs"]), 2)
        pooled = self.client.get(f"/experiments/{exp.id}/?pool=1")
        self.assertTrue(pooled.context["pool"])
