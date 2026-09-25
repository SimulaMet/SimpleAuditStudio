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

    def _create_scenario(self, title="Dose guidance", description="Ask about medication dose.", expected_behavior=["Give safe guidance"]):
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

    def test_duplicate_endpoint_display_name_returns_409(self):
        body = {
            "display_name": "Same Name",
            "provider": "openai",
            "base_url": "http://mock-model:8901/v1",
            "model_id": "m",
        }
        first = self.client.post(f"/api/projects/{self.project.id}/model-endpoints/create/", body, format="json")
        assert first.status_code == 201, first.content
        second = self.client.post(f"/api/projects/{self.project.id}/model-endpoints/create/", body, format="json")
        assert second.status_code == 409, second.content
        assert second.json()["error"]["code"] in {"duplicate_display_name", "conflict"}
