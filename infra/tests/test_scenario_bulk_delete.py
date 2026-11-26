"""Bulk delete on the Scenarios page: several scenarios leave a set in one new version."""
from django.contrib.messages import get_messages
from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from scenarios.models import ScenarioSet
from scenarios.services import create_scenario, publish_scenario_set_version


class ScenarioBulkDeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bd", password="pass12345")
        self.project = Project.objects.create(name="BD", slug="bd")
        self.membership = ProjectMembership.objects.create(
            project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN
        )
        self.sset = ScenarioSet.objects.create(project=self.project, name="Set")
        self.scenarios = [self._scenario(f"s{n}") for n in range(5)]
        self._publish(self.sset, self.scenarios)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def _scenario(self, key, project=None):
        return create_scenario(project=project or self.project, user=self.user, key=key, title=key.upper(),
                               description="d", expected_behavior=["x"])

    def _publish(self, sset, scenarios):
        return publish_scenario_set_version(scenario_set=sset, user=self.user, scenario_ids=[s.id for s in scenarios])

    def _bulk_delete(self, scenarios, extra_ids=()):
        ids = [str(s.id) for s in scenarios] + list(extra_ids)
        return self.client.post(f"/scenarios/bulk-delete/{self.sset.id}/", {"scenario_ids": ids})

    def _latest_ids(self, sset=None):
        latest = (sset or self.sset).versions.order_by("-version").first()
        return list(latest.items.order_by("position").values_list("scenario_id", flat=True))

    @staticmethod
    def _messages(response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def _archived(self, scenarios):
        for scenario in scenarios:
            scenario.refresh_from_db()
        return [scenario.archived_at is not None for scenario in scenarios]

    def test_three_of_five_leave_the_set_in_one_new_version(self):
        gone, kept = self.scenarios[:3], self.scenarios[3:]

        response = self._bulk_delete(gone)

        self.assertRedirects(response, f"/scenarios/?set={self.sset.id}", fetch_redirect_response=False)
        self.assertEqual(self._messages(response), ["3 scenarios deleted (published v2)."])
        self.assertEqual(self.sset.versions.count(), 2)
        self.assertEqual(self._latest_ids(), [s.id for s in kept])
        self.assertEqual(self._archived(gone), [True, True, True])
        self.assertEqual(self._archived(kept), [False, False])

    def test_scenario_another_set_still_uses_is_not_archived(self):
        shared, unshared = self.scenarios[0], self.scenarios[1]
        other = ScenarioSet.objects.create(project=self.project, name="Other")
        self._publish(other, [shared])

        self._bulk_delete([shared, unshared])

        self.assertEqual(self._latest_ids(), [s.id for s in self.scenarios[2:]])
        self.assertEqual(self._archived([shared, unshared]), [False, True])
        self.assertEqual(other.versions.count(), 1)
        self.assertEqual(self._latest_ids(other), [shared.id])

    def test_viewer_cannot_bulk_delete(self):
        self.membership.role = ProjectMembership.Role.VIEWER
        self.membership.save()

        self._bulk_delete(self.scenarios[:2])

        self.assertEqual(self.sset.versions.count(), 1)
        self.assertEqual(self._archived(self.scenarios), [False] * 5)

    def test_ids_outside_the_set_or_the_workspace_have_no_effect(self):
        loose = self._scenario("loose")   # same workspace, not in this set
        elsewhere = Project.objects.create(name="Else", slug="else")
        ProjectMembership.objects.create(project=elsewhere, user=self.user, role=ProjectMembership.Role.ADMIN)
        foreign = self._scenario("foreign", elsewhere)

        response = self._bulk_delete([loose, foreign], extra_ids=["abc", ""])

        self.assertEqual(self._messages(response), ["Select at least one scenario of this set to delete."])
        self.assertEqual(self.sset.versions.count(), 1)
        self.assertEqual(self._archived([loose, foreign]), [False, False])

        self._bulk_delete([loose, foreign, self.scenarios[0]])

        self.assertEqual(self.sset.versions.count(), 2)
        self.assertEqual(self._latest_ids(), [s.id for s in self.scenarios[1:]])
        self.assertEqual(self._archived([loose, foreign, self.scenarios[0]]), [False, False, True])

    def test_deleting_every_scenario_behaves_like_deleting_the_last_one(self):
        # Single delete of a set's last scenario publishes no (empty) version:
        # the scenario is archived and the latest version still lists it.
        single = ScenarioSet.objects.create(project=self.project, name="Single")
        last = self._scenario("last")
        self._publish(single, [last])
        self.client.post(f"/scenarios/delete/{last.id}/", {"set_id": str(single.id)})
        self.assertEqual(single.versions.count(), 1)
        self.assertEqual(self._archived([last]), [True])

        response = self._bulk_delete(self.scenarios)

        self.assertEqual(self._messages(response), ["5 scenarios deleted."])
        self.assertEqual(self.sset.versions.count(), 1)
        self.assertEqual(self._archived(self.scenarios), [True] * 5)

    def test_single_delete_still_removes_one_scenario(self):
        self.client.post(f"/scenarios/delete/{self.scenarios[0].id}/", {"set_id": str(self.sset.id)})

        self.assertEqual(self.sset.versions.count(), 2)
        self.assertEqual(self._latest_ids(), [s.id for s in self.scenarios[1:]])
        self.assertEqual(self._archived(self.scenarios), [True, False, False, False, False])

    def test_page_offers_bulk_delete_on_the_latest_version_only(self):
        action = f'action="/scenarios/bulk-delete/{self.sset.id}/"'
        checkbox = 'form="bulk-delete-form" name="scenario_ids"'
        latest = self.client.get(f"/scenarios/?set={self.sset.id}").content.decode()
        self.assertIn(action, latest)
        self.assertEqual(latest.count(checkbox), 5)

        self._bulk_delete(self.scenarios[:1])
        older = self.client.get(f"/scenarios/?set={self.sset.id}&version=1").content.decode()
        self.assertNotIn(action, older)
        self.assertNotIn(checkbox, older)
