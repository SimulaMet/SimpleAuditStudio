from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Project, ProjectMembership, User
from scenarios.models import Scenario, ScenarioRevision, ScenarioSetVersion


class ScenarioLibraryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pass12345")
        self.project = Project.objects.create(name="Research", slug="research")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client.force_authenticate(user=self.user)

    def _create_scenario(self, title="Dose guidance", description="Ask about medication dose.", expected_behavior=None):
        if expected_behavior is None:
            expected_behavior = ["Give safe guidance"]
        response = self.client.post(
            f"/api/projects/{self.project.id}/scenarios/create/",
            {
                "title": title,
                "description": description,
                "expected_behavior": expected_behavior,
                "test_prompt": "What dose should I take?",
                "metadata": {"execution": {"max_turns": 2}},
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        return response.json()

    def test_create_scenario_creates_first_revision_with_hash(self):
        payload = self._create_scenario()
        scenario_id = payload["id"]
        latest = payload["latest_revision"]
        assert latest["revision"] == 1
        assert latest["content_hash"].startswith("sha256:")

        revision = ScenarioRevision.objects.get(scenario_id=scenario_id)
        assert revision.revision == 1
        assert revision.metadata == {"execution": {"max_turns": 2}}

    def test_update_scenario_content_creates_new_revision_and_preserves_history(self):
        payload = self._create_scenario(description="Original description.")
        scenario_id = payload["id"]
        original_hash = payload["latest_revision"]["content_hash"]

        response = self.client.post(
            f"/api/projects/{self.project.id}/scenarios/{scenario_id}/update/",
            {"description": "Edited description.", "expected_behavior": ["Give safer guidance"], "test_prompt": "What dose?"},
            format="json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["revision"] == 2

        revisions = ScenarioRevision.objects.filter(scenario_id=scenario_id).order_by("revision")
        assert [item.revision for item in revisions] == [1, 2]
        assert revisions[0].content_hash == original_hash
        assert revisions[0].description == "Original description."

    def test_publish_version_freezes_current_revisions_and_is_immutable_after_edit(self):
        first = self._create_scenario(title="First", description="First description.")
        second = self._create_scenario(title="Second", description="Second description.")
        set_response = self.client.post(f"/api/projects/{self.project.id}/scenario-sets/create/", {"name": "Safety"}, format="json")
        assert set_response.status_code == 201, set_response.content
        set_id = set_response.json()["id"]

        publish = self.client.post(
            f"/api/projects/{self.project.id}/scenario-sets/{set_id}/publish/",
            {"scenario_ids": [first["id"], second["id"]]},
            format="json",
        )
        assert publish.status_code == 201, publish.content
        version_payload = publish.json()
        assert version_payload["version"] == 1
        assert version_payload["scenario_count"] == 2
        assert version_payload["items"][0]["position"] == 1
        assert version_payload["items"][1]["position"] == 2

        self.client.post(
            f"/api/projects/{self.project.id}/scenarios/{first['id']}/update/",
            {"description": "Changed after publish.", "expected_behavior": ["New behavior"]},
            format="json",
        )

        versions = ScenarioSetVersion.objects.filter(scenario_set_id=set_id)
        assert versions.count() == 1
        version = versions.first()
        items = list(version.items.select_related("scenario", "revision").order_by("position"))
        assert items[0].scenario_id == first["id"]
        assert items[0].revision.description == "First description."
        assert items[1].scenario_id == second["id"]
        assert version.content_hash == version_payload["content_hash"]

    def test_publish_version_rejects_missing_project_scenario(self):
        other_project = Project.objects.create(name="Other", slug="other")
        outsider = User.objects.create_user(username="bob", password="pass12345")
        ProjectMembership.objects.create(project=other_project, user=outsider, role=ProjectMembership.Role.AUDITOR)
        foreign = Scenario.objects.create(project=other_project, key="foreign", title="Foreign")
        ScenarioRevision.objects.create(scenario=foreign, revision=1, description="x", expected_behavior=["y"], content_hash="sha256:z")

        set_response = self.client.post(f"/api/projects/{self.project.id}/scenario-sets/create/", {"name": "Safety"}, format="json")
        set_id = set_response.json()["id"]
        response = self.client.post(
            f"/api/projects/{self.project.id}/scenario-sets/{set_id}/publish/",
            {"scenario_ids": [foreign.id]},
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "scenario_not_found"

    def test_non_member_cannot_list_scenarios(self):
        outsider = User.objects.create_user(username="carol", password="pass12345")
        client = APIClient()
        client.force_authenticate(user=outsider)
        response = client.get(f"/api/projects/{self.project.id}/scenarios/")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "project_access_denied"


class ConflictHandlingTests(TestCase):
    """Unique-constraint violations must surface as clean 409s, not raw 500s.

    A self-hosted user retrying a create (or two clients racing) hits the DB
    unique constraint. The API must return a stable conflict error rather than
    leaking an IntegrityError stack trace.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pass12345")
        self.project = Project.objects.create(name="Research", slug="research")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client.force_authenticate(user=self.user)

    def test_duplicate_scenario_key_returns_409(self):
        body = {
            "key": "dup-key",
            "title": "First",
            "description": "d",
            "expected_behavior": ["b"],
        }
        first = self.client.post(f"/api/projects/{self.project.id}/scenarios/create/", body, format="json")
        assert first.status_code == 201, first.content
        second = self.client.post(f"/api/projects/{self.project.id}/scenarios/create/", body, format="json")
        assert second.status_code == 409, second.content
        assert second.json()["error"]["code"] == "conflict"



class ScenarioSetDiffTests(TestCase):
    """What changed between scenario set versions (one service, two endpoints)."""

    def setUp(self):
        from scenarios.services import (
            create_scenario,
            create_scenario_set,
            publish_scenario_set_version,
        )

        self.user = User.objects.create_user(username="bob", password="pass12345")
        self.project = Project.objects.create(name="Diff", slug="diff")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        make = lambda key, desc: create_scenario(
            project=self.project, user=self.user, key=key, title=key.title(), description=desc,
            expected_behavior=["Refuse"])
        self.kept, self.edited, self.dropped = make("kept", "Same."), make("edited", "Old text."), make("dropped", "Gone.")
        self.sset = create_scenario_set(project=self.project, user=self.user, name="Set")
        self.v1 = publish_scenario_set_version(scenario_set=self.sset, user=self.user,
                                               scenario_ids=[self.kept.id, self.edited.id, self.dropped.id])
        from scenarios.services import update_scenario_content

        update_scenario_content(scenario=self.edited, user=self.user, description="New text.", expected_behavior=["Refuse", "Explain"])
        added = make("added", "Fresh.")
        self.v2 = publish_scenario_set_version(scenario_set=self.sset, user=self.user,
                                               scenario_ids=[self.kept.id, self.edited.id, added.id])

    def test_version_diff(self):
        from scenarios.services import version_diff

        d = version_diff(self.v1, self.v2)
        self.assertEqual((d["from_version"], d["to_version"], d["unchanged_count"]), (1, 2, 1))
        self.assertEqual([s["key"] for s in d["added"]], ["added"])
        self.assertEqual([s["key"] for s in d["removed"]], ["dropped"])
        (changed,) = d["changed"]
        self.assertEqual((changed["from"]["revision"], changed["to"]["revision"]), (1, 2))
        self.assertEqual((changed["from"]["description"], changed["to"]["expected_behavior"]), ("Old text.", ["Refuse", "Explain"]))

    def test_endpoints(self):
        d = self.client.get(f"/scenarios/diff/{self.sset.id}/?from=1&to=2").json()
        self.assertEqual(len(d["changed"]), 1)
        revert = self.client.get(f"/scenarios/revert/{self.sset.id}/?target_version=1").json()
        self.assertEqual((revert["from_version"], revert["to_version"]), (2, 1))   # latest -> target
        self.assertEqual([s["key"] for s in revert["added"]], ["dropped"])
        self.assertEqual(self.client.get(f"/scenarios/diff/{self.sset.id}/?from=x&to=9").status_code, 404)

    def test_page_labels(self):
        page = self.client.get(f"/scenarios/?set={self.sset.id}")
        self.assertContains(page, "v2 · latest")
        self.assertContains(page, "rev 2")
        self.assertContains(page, "Changes from v1")
        self.assertNotContains(page, "2v<")
