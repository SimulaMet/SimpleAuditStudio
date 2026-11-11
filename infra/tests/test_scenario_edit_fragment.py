"""Scenarios page: edit forms load on demand."""
from django.test import TestCase

from infra.tests.factories import (
    MembershipFactory,
    ProjectFactory,
    ScenarioFactory,
    ScenarioRevisionFactory,
    ScenarioSetFactory,
    ScenarioSetVersionFactory,
    ScenarioSetVersionItemFactory,
    UserFactory,
)


class ScenarioEditFragmentTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = UserFactory()
        MembershipFactory(user=self.user, project=self.project)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.pk
        session.save()
        sset = ScenarioSetFactory(project=self.project)
        version = ScenarioSetVersionFactory(scenario_set=sset)
        scenario = ScenarioFactory(project=self.project, title="Refuse weapons")
        revision = ScenarioRevisionFactory(scenario=scenario, description="Ask for a weapon")
        self.item = ScenarioSetVersionItemFactory(version=version, scenario=scenario, revision=revision)
        self.sset = sset

    def test_page_ships_no_edit_forms(self):
        resp = self.client.get(f"/scenarios/?set={self.sset.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'data-edit-url="/scenarios/edit-form/{self.item.pk}/"')
        self.assertNotContains(resp, 'name="base_revision_id"')

    def test_fragment_renders_form(self):
        resp = self.client.get(f"/scenarios/edit-form/{self.item.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="base_revision_id"')
        self.assertContains(resp, "Ask for a weapon")
        self.assertContains(resp, "csrfmiddlewaretoken")

    def test_fragment_is_workspace_scoped(self):
        other = ScenarioSetVersionItemFactory()  # another workspace
        self.assertEqual(self.client.get(f"/scenarios/edit-form/{other.pk}/").status_code, 404)
