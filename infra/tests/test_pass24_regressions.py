"""Regression tests for audit pass 24: agent rows used as auditor/judge, edits against the pinned revision."""
from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from infra.tests.factories import AgentFactory, AuditRunFactory, ProjectFactory
from model_registry.services import agent_target_model


class AgentRowAuditorRoleTest(TestCase):
    def test_deleted_agent_row_used_as_auditor_is_not_repointed(self):
        project = ProjectFactory()
        a = AgentFactory(project=project, external_id="studio.a")
        ref_a = agent_target_model(a)
        run = AuditRunFactory(project=project, auditor_model=ref_a)
        a.delete()
        b = AgentFactory(project=project, external_id="studio.b")
        ref_b = agent_target_model(b)
        ref_a.refresh_from_db()
        run.refresh_from_db()
        self.assertNotEqual(ref_a.pk, ref_b.pk)
        self.assertEqual(run.auditor_model.model_id, "studio.a")

    def test_deleted_agent_row_used_as_judge_is_not_repointed(self):
        project = ProjectFactory()
        a = AgentFactory(project=project, external_id="studio.a")
        ref_a = agent_target_model(a)
        AuditRunFactory(project=project, judge_model=ref_a)
        a.delete()
        b = AgentFactory(project=project, external_id="studio.b")
        agent_target_model(b)
        ref_a.refresh_from_db()
        self.assertEqual(ref_a.model_id, "studio.a")


class EditBackToLatestTest(TestCase):
    def setUp(self):
        from scenarios.models import ScenarioSet
        from scenarios.services import create_scenario

        self.user = User.objects.create_user(username="rv", password="pass12345")
        self.project = Project.objects.create(name="RV", slug="rv")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sa = ScenarioSet.objects.create(project=self.project, name="A")
        self.sb = ScenarioSet.objects.create(project=self.project, name="B")
        self.s = create_scenario(project=self.project, user=self.user, key="s", title="S", description="old",
                                 expected_behavior=["x"])
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def _desc(self, sset):
        latest = sset.versions.order_by("-version").first()
        return {i.scenario_id: i.revision.description for i in latest.items.select_related("revision")}[self.s.id]

    def test_edit_in_other_set_to_latest_text_is_published(self):
        from scenarios.services import publish_scenario_set_version

        publish_scenario_set_version(scenario_set=self.sa, user=self.user, scenario_ids=[self.s.id])
        publish_scenario_set_version(scenario_set=self.sb, user=self.user, scenario_ids=[self.s.id])
        # Edit from set A -> rev2 "new"
        self.client.post(f"/scenarios/edit/{self.s.id}/", {
            "title": "S", "description": "new", "expected_behavior": "x", "set_id": str(self.sa.id)})
        self.assertEqual(self._desc(self.sa), "new")
        self.assertEqual(self._desc(self.sb), "old")
        # In set B (showing "old") the user types "new" and saves.
        self.client.post(f"/scenarios/edit/{self.s.id}/", {
            "title": "S", "description": "new", "expected_behavior": "x", "set_id": str(self.sb.id)})
        self.assertEqual(self._desc(self.sb), "new")

    def test_after_revert_edit_back_is_published(self):
        from scenarios.services import publish_scenario_set_version

        publish_scenario_set_version(scenario_set=self.sa, user=self.user, scenario_ids=[self.s.id])
        self.client.post(f"/scenarios/edit/{self.s.id}/", {
            "title": "S", "description": "new", "expected_behavior": "x", "set_id": str(self.sa.id)})
        self.client.post(f"/scenarios/revert/{self.sa.id}/", {"target_version": "1"})
        self.assertEqual(self._desc(self.sa), "old")
        self.client.post(f"/scenarios/edit/{self.s.id}/", {
            "title": "S", "description": "new", "expected_behavior": "x", "set_id": str(self.sa.id)})
        self.assertEqual(self._desc(self.sa), "new")


class StaleScenarioFormTest(TestCase):
    """A form opened on an older revision must not republish its stale text."""

    def setUp(self):
        from scenarios.models import ScenarioSet
        from scenarios.services import create_scenario, publish_scenario_set_version

        self.user = User.objects.create_user(username="ov", password="pass12345")
        self.project = Project.objects.create(name="OV", slug="ov")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sa = ScenarioSet.objects.create(project=self.project, name="A")
        self.s = create_scenario(project=self.project, user=self.user, key="s", title="S", description="old",
                                 expected_behavior=["x"])
        publish_scenario_set_version(scenario_set=self.sa, user=self.user, scenario_ids=[self.s.id])
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_stale_form_is_refused_and_old_version_view_has_no_edit_form(self):
        v1_rev = self.sa.versions.get(version=1).items.get().revision_id
        self.client.post(f"/scenarios/edit/{self.s.id}/", {
            "title": "S", "description": "new", "expected_behavior": "x", "set_id": str(self.sa.id),
            "base_revision_id": str(v1_rev)})
        self.assertEqual(self.sa.versions.count(), 2)
        html = self.client.get(f"/scenarios/?set={self.sa.id}&version=1").content.decode()
        self.assertNotIn(f"openEditForm({self.s.id}", html)
        # A stale form (opened on v1) posting a title-only change must not revert content.
        self.client.post(f"/scenarios/edit/{self.s.id}/", {
            "title": "S renamed", "description": "old", "expected_behavior": "x", "set_id": str(self.sa.id),
            "base_revision_id": str(v1_rev)})
        latest = self.sa.versions.order_by("-version").first()
        self.assertEqual(latest.items.get(scenario=self.s).revision.description, "new")


class StaleJudgeFormTest(TestCase):
    def setUp(self):
        from judges.services import create_judge

        self.user = User.objects.create_user(username="sj", password="pass12345")
        self.project = Project.objects.create(name="SJ", slug="sj")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.judge = create_judge(project=self.project, name="J1", user=self.user, output="severity",
                                  criteria="old criteria", options={})
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def _form(self, base, **kw):
        data = {"name": "J1", "description": "", "start": "format:severity", "criteria": "old criteria",
                "probe_prompt": "", "dimensions": "", "question": "", "pass_when": "yes", "note": "",
                "base_version_id": str(base)}
        data.update(kw)
        return data

    def test_stale_form_cannot_revert_newer_criteria(self):
        v1 = self.judge.latest.pk
        self.client.post(f"/judges/{self.judge.id}/", self._form(v1, criteria="NEW strict criteria"))
        self.assertEqual(self.judge.latest.criteria, "NEW strict criteria")
        self.client.post(f"/judges/{self.judge.id}/", self._form(v1, name="J1 renamed"))
        self.judge.refresh_from_db()
        self.assertEqual(self.judge.latest.criteria, "NEW strict criteria")
        self.assertEqual(self.judge.name, "J1")   # rename waits for the version check

    def test_refused_save_reloads_latest_instead_of_arming_stale_form(self):
        v1 = self.judge.latest.pk
        self.client.post(f"/judges/{self.judge.id}/", self._form(v1, criteria="NEW strict criteria"))
        resp = self.client.post(f"/judges/{self.judge.id}/", self._form(v1, name="J1 renamed"))
        self.assertEqual(resp.status_code, 302)
        page = self.client.get(resp["Location"]).content.decode()
        self.assertIn("NEW strict criteria", page)
        self.assertNotIn(">old criteria<", page)

    def test_explicit_restore_of_old_version_still_works(self):
        v1 = self.judge.latest.pk
        self.client.post(f"/judges/{self.judge.id}/", self._form(v1, criteria="NEW"))
        self.client.post(f"/judges/{self.judge.id}/", {**self._form(v1), "restore": "1"})
        self.assertEqual(self.judge.latest.criteria, "old criteria")


class PublicRunPageUrlTest(TestCase):
    def test_anonymous_public_run_page_hides_endpoint_urls(self):
        from audits.models import AuditRun, Experiment
        from infra.tests.factories import AuditRunFactory, ProjectFactory

        project = ProjectFactory()
        experiment = Experiment.objects.create(project=project, name="Pub", visibility=Experiment.Visibility.PUBLIC)
        run = AuditRunFactory(project=project, status="completed")
        snap = {**(run.target_config_snapshot or {}), "base_url": "http://frozen-internal.corp/v1"}
        AuditRun.objects.filter(pk=run.pk).update(experiment=experiment, target_config_snapshot=snap)
        run.target_model.connection.base_url = "http://live-changed.corp/v1"
        run.target_model.connection.save()
        html = self.client.get(f"/runs/{run.id}/").content.decode()
        self.assertNotIn("frozen-internal.corp", html)
        self.assertNotIn("live-changed.corp", html)


class PublicRunAgentUrlTest(TestCase):
    def test_anonymous_public_run_page_hides_agent_base_url(self):
        from audits.models import AuditRun, Experiment
        from infra.tests.factories import AuditRunFactory, ProjectFactory

        project = ProjectFactory()
        experiment = Experiment.objects.create(project=project, name="Pub", visibility=Experiment.Visibility.PUBLIC)
        run = AuditRunFactory(project=project, status="completed")
        agent_snapshot = {"name": "Ag", "base_model": {"model_id": "m", "display_name": "M", "connection_id": 1,
                                                       "base_url": "http://agent-base.corp/v1",
                                                       "connection": {"base_url": "http://agent-base.corp/v1"}}}
        AuditRun.objects.filter(pk=run.pk).update(experiment=experiment, agent_config_snapshot=agent_snapshot)
        html = self.client.get(f"/runs/{run.id}/").content.decode()
        self.assertNotIn("agent-base.corp", html)


class RepeatedResultErrorTextTest(TestCase):
    def test_error_read_from_reps(self):
        from infra.worker import _is_permanent_error, result_error_text

        payload = {"reps": [{"severity": "ERROR", "judgment": {"error": "BadRequestError: Error code: 400 invalid model"}},
                            {"severity": "ERROR", "summary": "x"}], "aggregated_severity": "ERROR"}
        text = result_error_text(payload)
        self.assertIn("400", text)
        self.assertTrue(_is_permanent_error(text))
        self.assertEqual(result_error_text({"judgment": {"error": "boom"}}), "boom")
        self.assertEqual(result_error_text(None), "")
