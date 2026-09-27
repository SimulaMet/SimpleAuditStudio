"""Recurring audits: schedule tick, drift statistics and schedule pages.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_audit_schedules
"""
from datetime import UTC, timedelta
from unittest import mock

from django.test import Client, TestCase
from django.utils import timezone

from audits.models import AuditRun, AuditSchedule
from audits.scheduling import (
    advance,
    drift_series,
    run_due_schedules,
    two_proportion_z,
    wilson,
)
from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
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


class ScheduleTestBase(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.sset = ScenarioSetFactory(project=self.project)
        self.v1 = ScenarioSetVersionFactory(scenario_set=self.sset, version=1)
        self.model = RegisteredModelFactory(project=self.project)
        self.now = timezone.now()

        patcher = mock.patch("audits.services.resolve_engine_provenance", return_value=_PROVENANCE)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No Hatchet in tests: submission is exercised separately.
        submit = mock.patch("audits.services.submit_audit_run")
        self.submit = submit.start()
        self.addCleanup(submit.stop)

    def _schedule(self, **kw):
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
        return AuditSchedule.objects.create(**defaults)


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


class TickTests(ScheduleTestBase):
    def test_due_schedule_launches_frozen_run_and_advances(self):
        s = self._schedule()
        ids = run_due_schedules(now=self.now)
        self.assertEqual(len(ids), 1)
        run = AuditRun.objects.get(pk=ids[0])
        self.assertEqual(run.schedule_id, s.id)
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
        self._schedule(next_run_at=self.now + timedelta(hours=1))
        self._schedule(name="Paused", enabled=False)
        self.assertEqual(run_due_schedules(now=self.now), [])

    def test_skips_while_previous_run_in_flight(self):
        s = self._schedule()
        active = AuditRunFactory(project=self.project, status=AuditRun.Status.JUDGING, schedule=s)
        s.last_run = active
        s.save()
        self.assertEqual(run_due_schedules(now=self.now), [])
        s.refresh_from_db()
        self.assertIn("still judging", s.last_error)
        self.assertGreater(s.next_run_at, self.now)

    def test_unpinned_follows_latest_version(self):
        v2 = ScenarioSetVersionFactory(scenario_set=self.sset, version=2)
        self._schedule(scenario_set_version=None)
        run = AuditRun.objects.get(pk=run_due_schedules(now=self.now)[0])
        self.assertEqual(run.scenario_set_version_id, v2.id)

    def test_failure_is_recorded_and_still_advances(self):
        empty_set = ScenarioSetFactory(project=self.project)
        s = self._schedule(scenario_set=empty_set, scenario_set_version=None)
        self.assertEqual(run_due_schedules(now=self.now), [])
        s.refresh_from_db()
        self.assertIn("no published version", s.last_error)
        self.assertTrue(s.enabled)
        self.assertGreater(s.next_run_at, self.now)

    def test_deleted_owner_pauses_schedule(self):
        s = self._schedule(created_by=None)
        self.assertEqual(run_due_schedules(now=self.now), [])
        s.refresh_from_db()
        self.assertFalse(s.enabled)
        self.assertIn("deleted user", s.last_error)

    def test_submits_created_runs(self):
        self._schedule()
        with mock.patch("audits.services.submit_audit_run") as submit:
            ids = run_due_schedules(now=self.now)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0].pk, ids[0])


class StatsTests(ScheduleTestBase):
    def test_wilson_bounds(self):
        lo, hi = wilson(50, 100)
        self.assertAlmostEqual(lo, 0.404, places=2)
        self.assertAlmostEqual(hi, 0.596, places=2)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))

    def test_two_proportion_z_sign(self):
        self.assertLess(two_proportion_z(90, 100, 60, 100), -1.96)
        self.assertIsNone(two_proportion_z(0, 0, 1, 2))

    def _run(self, s, severities_per_scenario, version=None):
        run = AuditRunFactory(project=self.project, schedule=s, scenario_set_version=version or self.v1)
        for i, sev in enumerate(severities_per_scenario):
            ScenarioResultFactory(run_id=run.pk, version_item_id=str(i), result=_result(sev))
        return run

    def test_drift_flags_significant_drop_only(self):
        s = self._schedule()
        passing = [["pass"] * 5] * 10  # 50/50 trials pass
        for _ in range(3):
            self._run(s, passing)
        self._run(s, [["pass"] * 5] * 9 + [["pass"] * 4 + ["high"]])  # 49/50: noise
        self._run(s, [["pass", "high", "high", "high", "high"]] * 10)  # 10/50: drop
        points = drift_series(s)
        self.assertEqual([p["change"] for p in points], ["", "", "", "", "drop"])
        self.assertEqual(points[0]["n"], 50)

    def test_errors_excluded_from_trials(self):
        s = self._schedule()
        self._run(s, [["pass", "ERROR"], ["high"]])
        p = drift_series(s)[0]
        self.assertEqual((p["k"], p["n"], p["errors"]), (1, 2, 1))

    def test_version_change_resets_baseline(self):
        s = self._schedule()
        v2 = ScenarioSetVersionFactory(scenario_set=self.sset, version=2)
        self._run(s, [["pass"] * 5] * 10)
        self._run(s, [["high"] * 5] * 10, version=v2)
        points = drift_series(s)
        self.assertTrue(points[1]["version_changed"])
        self.assertEqual(points[1]["change"], "")


class SchedulePagesTests(ScheduleTestBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")

    def _post_create(self, **extra):
        data = {
            "name": "Nightly",
            "interval_hours": "24",
            "scenario_set": self.sset.id,
            "pin_version": "1",
            "target_model": self.model.id,
            "auditor_model": self.model.id,
            "judge_model": self.model.id,
            "max_turns": "4",
            "n_repetitions": "3",
            "gen_config_json": '{"judge_params": {"temperature": 0}}',
        }
        data.update(extra)
        return self.client.post("/schedules/", data)

    def test_create_schedule(self):
        resp = self._post_create()
        self.assertEqual(resp.status_code, 302)
        s = AuditSchedule.objects.get(name="Nightly")
        self.assertEqual(s.interval_hours, 24)
        self.assertEqual(s.scenario_set_version_id, self.v1.id)
        self.assertEqual(
            s.generation_parameters,
            {"max_turns": 4, "n_repetitions": 3, "judge_params": {"temperature": 0}},
        )
        self.assertEqual(s.created_by, self.user)

    def test_create_rejects_foreign_model(self):
        foreign = RegisteredModelFactory()
        resp = self._post_create(target_model=foreign.id)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AuditSchedule.objects.exists())

    def test_pages_render(self):
        s = self._schedule()
        run = AuditRunFactory(project=self.project, schedule=s, scenario_set_version=self.v1)
        ScenarioResultFactory(run_id=run.pk, result=_result(["pass", "high"]))
        self.assertEqual(self.client.get("/schedules/").status_code, 200)
        resp = self.client.get(f"/schedules/{s.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<polyline")

    def test_actions(self):
        s = self._schedule()
        self.client.post(f"/schedules/{s.id}/toggle/")
        s.refresh_from_db()
        self.assertFalse(s.enabled)

        with mock.patch("infra.ui.submit_audit_run") as submit:
            resp = self.client.post(f"/schedules/{s.id}/run-now/")
        run = AuditRun.objects.get(schedule=s)
        self.assertRedirects(resp, f"/audits/{run.id}/", fetch_redirect_response=False)
        submit.assert_called_once()

        self.client.post(f"/schedules/{s.id}/delete/")
        self.assertFalse(AuditSchedule.objects.filter(pk=s.id).exists())
        run.refresh_from_db()
        self.assertIsNone(run.schedule_id)  # runs outlive their schedule

    def test_other_project_schedule_is_404(self):
        other_set = ScenarioSetFactory()
        other_model = RegisteredModelFactory(project=other_set.project)
        s = self._schedule(
            project=other_set.project,
            scenario_set=other_set,
            scenario_set_version=None,
            target_model=other_model,
            auditor_model=other_model,
            judge_model=other_model,
        )
        self.assertEqual(self.client.get(f"/schedules/{s.id}/").status_code, 404)
        self.assertEqual(self.client.post(f"/schedules/{s.id}/delete/").status_code, 404)


class SchedulePermissionTests(ScheduleTestBase):
    """View: any member. Create: admin/auditor. Manage: admin or creating auditor."""

    def _member(self, role):
        user = UserFactory()
        user.set_password("pw")
        user.save()
        MembershipFactory(user=user, project=self.project, role=role)
        client = Client()
        client.login(username=user.username, password="pw")
        return user, client

    def _create_payload(self, name="X", interval="168"):
        return {
            "name": name,
            "interval_hours": interval,
            "scenario_set": self.sset.id,
            "target_model": self.model.id,
            "auditor_model": self.model.id,
            "judge_model": self.model.id,
        }

    def test_viewer_can_view_but_not_create_or_manage(self):
        s = self._schedule()
        _, client = self._member("viewer")
        self.assertEqual(client.get("/schedules/").status_code, 200)
        self.assertEqual(client.get(f"/schedules/{s.id}/").status_code, 200)
        self.assertEqual(client.post("/schedules/", self._create_payload()).status_code, 403)
        for action in ("toggle", "run-now", "delete"):
            self.assertEqual(client.post(f"/schedules/{s.id}/{action}/").status_code, 403)
        s.refresh_from_db()
        self.assertTrue(s.enabled)
        self.assertEqual(AuditSchedule.objects.count(), 1)

    def test_auditor_creates_and_manages_own_but_not_others(self):
        others = self._schedule()  # created by the admin
        auditor, client = self._member("auditor")
        self.assertEqual(client.post("/schedules/", self._create_payload(name="Mine")).status_code, 302)
        mine = AuditSchedule.objects.get(name="Mine")
        self.assertEqual(mine.created_by, auditor)
        self.assertEqual(client.post(f"/schedules/{mine.id}/toggle/").status_code, 302)
        self.assertEqual(client.post(f"/schedules/{others.id}/toggle/").status_code, 403)
        self.assertEqual(client.post(f"/schedules/{others.id}/delete/").status_code, 403)

    def test_admin_manages_any_schedule(self):
        auditor, _ = self._member("auditor")
        s = self._schedule(created_by=auditor)
        _, client = self._member("admin")
        self.assertEqual(client.post(f"/schedules/{s.id}/delete/").status_code, 302)
        self.assertFalse(AuditSchedule.objects.filter(pk=s.id).exists())

    def test_interval_minimum_and_cap(self):
        _, client = self._member("admin")
        resp = client.post("/schedules/", self._create_payload(interval="1"))
        self.assertContains(resp, "between 6 hours")
        from audits.scheduling import MAX_SCHEDULES_PER_PROJECT

        for i in range(MAX_SCHEDULES_PER_PROJECT):
            self._schedule(name=f"S{i}")
        resp = client.post("/schedules/", self._create_payload(name="One too many"))
        self.assertContains(resp, "limit")
        self.assertFalse(AuditSchedule.objects.filter(name="One too many").exists())

    def test_tick_pauses_schedule_when_owner_downgraded(self):
        auditor, _ = self._member("auditor")
        s = self._schedule(created_by=auditor)
        auditor.memberships.filter(project=self.project).update(role="viewer")
        self.assertEqual(run_due_schedules(now=self.now), [])
        s.refresh_from_db()
        self.assertFalse(s.enabled)
        self.assertIn("no longer has admin or auditor role", s.last_error)

    def test_cannot_resume_schedule_of_downgraded_owner(self):
        auditor, _ = self._member("auditor")
        s = self._schedule(created_by=auditor, enabled=False)
        auditor.memberships.filter(project=self.project).update(role="viewer")
        _, admin_client = self._member("admin")
        admin_client.post(f"/schedules/{s.id}/toggle/")
        s.refresh_from_db()
        self.assertFalse(s.enabled)


class LaunchPermissionTests(ScheduleTestBase):
    def test_viewer_cannot_launch_one_off_audit(self):
        viewer = UserFactory()
        viewer.set_password("pw")
        viewer.save()
        MembershipFactory(user=viewer, project=self.project, role="viewer")
        client = Client()
        client.login(username=viewer.username, password="pw")
        page = client.get("/audits/new/")
        self.assertContains(page, "view-only access")
        resp = client.post("/audits/new/", {
            "scenario_set": self.sset.id,
            "target_model": self.model.id,
            "auditor_model": self.model.id,
            "judge_model": self.model.id,
        })
        self.assertContains(resp, "Insufficient project role")
        self.assertFalse(AuditRun.objects.exists())


class CronScheduleTests(ScheduleTestBase):
    def test_validate_cron_accepts_and_normalises(self):
        from audits.scheduling import validate_cron

        self.assertEqual(validate_cron("  0   6 * *  1 "), "0 6 * * 1")
        self.assertEqual(validate_cron("0 */6 * * *"), "0 */6 * * *")
        # No minimum frequency for cron schedules.
        self.assertEqual(validate_cron("* * * * *"), "* * * * *")
        self.assertEqual(validate_cron("0 0,1 * * *"), "0 0,1 * * *")

    def test_validate_cron_rejects_bad_syntax(self):
        from audits.scheduling import validate_cron

        for expr, msg in [
            ("61 * * * *", "Invalid cron"),
            ("0 6 * *", "5 fields"),
            ("0 0 30 2 *", "Invalid cron"),  # Feb 30 never fires
        ]:
            with self.subTest(expr=expr), self.assertRaisesMessage(ValueError, msg):
                validate_cron(expr)

    def test_tick_moves_to_next_cron_firing(self):
        from datetime import datetime

        now = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)  # a Sunday
        s = self._schedule(cron_expression="0 6 * * 1", next_run_at=now - timedelta(days=8))
        self.assertEqual(len(run_due_schedules(now=now)), 1)
        s.refresh_from_db()
        self.assertEqual(s.next_run_at, datetime(2026, 9, 28, 6, 0, tzinfo=UTC))

    def test_create_cron_schedule_via_form(self):
        client = Client()
        client.login(username=self.user.username, password="pw")
        payload = {
            "name": "Monday",
            "interval_hours": "cron",
            "cron_expression": "0 6 * * 1",
            "first_run_at": "2030-01-01T00:00",  # a Tuesday
            "scenario_set": self.sset.id,
            "target_model": self.model.id,
            "auditor_model": self.model.id,
            "judge_model": self.model.id,
        }
        self.assertEqual(client.post("/schedules/", payload).status_code, 302)
        s = AuditSchedule.objects.get(name="Monday")
        self.assertEqual(s.cron_expression, "0 6 * * 1")
        self.assertEqual(s.next_run_at.isoformat(), "2030-01-07T06:00:00+00:00")
        self.assertIn("cron 0 6 * * 1", s.interval_display)

        payload.update(name="Every 5 min", cron_expression="*/5 * * * *", first_run_at="")
        self.assertEqual(client.post("/schedules/", payload).status_code, 302)
        self.assertTrue(AuditSchedule.objects.filter(name="Every 5 min").exists())

        payload.update(name="Bad", cron_expression="0 25 * * *")
        self.assertContains(client.post("/schedules/", payload), "Invalid cron")


class RepeatOnScheduleTests(ScheduleTestBase):
    """'Repeat on schedule' from an audit detail page."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")
        self.run = AuditRunFactory(
            project=self.project,
            scenario_set_version=self.v1,
            target_model=self.model,
            auditor_model=self.model,
            judge_model=self.model,
            generation_parameters_snapshot={"max_turns": 3, "judge_params": {"temperature": 0}},
        )
        # A newer version exists: the schedule must still pin the run's v1.
        ScenarioSetVersionFactory(scenario_set=self.sset, version=2)

    def _payload(self, **extra):
        data = {
            "name": "Drift",
            "interval_hours": "168",
            "scenario_set": self.sset.id,
            "scenario_set_version": self.v1.id,
            "target_model": self.model.id,
            "auditor_model": self.model.id,
            "judge_model": self.model.id,
            "max_turns": "3",
            "gen_config_json": '{"judge_params": {"temperature": 0}}',
            "baseline_run": self.run.id,
            "attach_baseline": "1",
        }
        data.update(extra)
        return data

    def test_detail_page_offers_repeat_to_writers_only(self):
        page = self.client.get(f"/audits/{self.run.id}/")
        self.assertContains(page, f"/schedules/?from_run={self.run.id}")
        self.assertContains(page, "Repeat on schedule")

        viewer = UserFactory()
        viewer.set_password("pw")
        viewer.save()
        MembershipFactory(user=viewer, project=self.project, role="viewer")
        other = Client()
        other.login(username=viewer.username, password="pw")
        self.assertNotContains(other.get(f"/audits/{self.run.id}/"), "Repeat on schedule")

    def test_detail_page_links_existing_schedule(self):
        s = self._schedule(name="Weekly GPT")
        self.run.schedule = s
        self.run.save()
        page = self.client.get(f"/audits/{self.run.id}/")
        self.assertContains(page, f"/schedules/{s.id}/")
        self.assertContains(page, "Weekly GPT")
        self.assertNotContains(page, "Repeat on schedule")

    def test_prefill_from_run(self):
        page = self.client.get(f"/schedules/?from_run={self.run.id}")
        self.assertContains(page, f'name="scenario_set_version" value="{self.v1.id}"')
        self.assertContains(page, f'name="baseline_run" value="{self.run.id}"')
        self.assertContains(page, f"{self.model.display_name} · {self.sset.name} drift")
        self.assertContains(page, "&quot;temperature&quot;: 0")

    def test_create_pins_run_version_and_attaches_baseline(self):
        resp = self.client.post("/schedules/", self._payload())
        s = AuditSchedule.objects.get(name="Drift")
        self.assertRedirects(resp, f"/schedules/{s.id}/", fetch_redirect_response=False)
        self.assertEqual(s.scenario_set_version_id, self.v1.id)
        self.run.refresh_from_db()
        self.assertEqual(self.run.schedule_id, s.id)
        self.assertEqual(s.last_run_id, self.run.id)

    def test_changed_settings_do_not_attach_baseline(self):
        self.client.post("/schedules/", self._payload(max_turns="7"))
        self.run.refresh_from_db()
        self.assertIsNone(self.run.schedule_id)

    def test_unchecked_attach_leaves_run_standalone(self):
        payload = self._payload()
        del payload["attach_baseline"]
        self.client.post("/schedules/", payload)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.schedule_id)

    def test_version_from_other_set_rejected(self):
        other_version = ScenarioSetVersionFactory(scenario_set=ScenarioSetFactory(project=self.project))
        resp = self.client.post("/schedules/", self._payload(scenario_set_version=other_version.id))
        self.assertContains(resp, "does not belong to the selected set")
        self.assertFalse(AuditSchedule.objects.exists())


class ScheduleTimezoneTests(ScheduleTestBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")

    def _payload(self, **extra):
        data = {
            "name": "Oslo mornings",
            "interval_hours": "cron",
            "cron_expression": "0 6 * * *",
            "timezone": "Europe/Oslo",
            "first_run_at": "2030-01-10T12:00",
            "scenario_set": self.sset.id,
            "target_model": self.model.id,
            "auditor_model": self.model.id,
            "judge_model": self.model.id,
        }
        data.update(extra)
        return data

    def test_cron_and_first_run_read_in_schedule_timezone(self):
        self.assertEqual(self.client.post("/schedules/", self._payload()).status_code, 302)
        s = AuditSchedule.objects.get(name="Oslo mornings")
        self.assertEqual(s.timezone, "Europe/Oslo")
        # 06:00 Oslo on Jan 11 (CET, UTC+1) = 05:00 UTC.
        self.assertEqual(s.next_run_at.isoformat(), "2030-01-11T05:00:00+00:00")
        self.assertIn("(Europe/Oslo)", s.interval_display)

    def test_interval_first_run_read_in_schedule_timezone(self):
        self.client.post("/schedules/", self._payload(interval_hours="24", first_run_at="2030-07-01T09:00"))
        s = AuditSchedule.objects.get(name="Oslo mornings")
        # 09:00 Oslo in July (CEST, UTC+2) = 07:00 UTC.
        self.assertEqual(s.next_run_at.isoformat(), "2030-07-01T07:00:00+00:00")

    def test_unknown_timezone_rejected(self):
        resp = self.client.post("/schedules/", self._payload(timezone="Mars/Olympus"))
        self.assertContains(resp, "Unknown timezone")
        self.assertFalse(AuditSchedule.objects.exists())

    def test_cron_follows_daylight_saving(self):
        from datetime import datetime

        from audits.scheduling import cron_next

        # Oslo leaves summer time on 2026-10-25: 06:00 local moves from 04:00 to 05:00 UTC.
        before = cron_next("0 6 * * *", datetime(2026, 10, 23, 12, tzinfo=UTC), "Europe/Oslo")
        after = cron_next("0 6 * * *", datetime(2026, 10, 25, 12, tzinfo=UTC), "Europe/Oslo")
        self.assertEqual(before.isoformat(), "2026-10-24T04:00:00+00:00")
        self.assertEqual(after.isoformat(), "2026-10-26T05:00:00+00:00")

    def test_pages_render_local_time_markup(self):
        self._schedule(cron_expression="0 6 * * *", timezone="Europe/Oslo")
        page = self.client.get("/schedules/")
        self.assertContains(page, "<time data-local datetime=")
        self.assertContains(page, 'name="timezone"')
