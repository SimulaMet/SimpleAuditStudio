"""Tests for scenario import/export endpoints."""

from rest_framework.test import APITestCase

from accounts.models import Project, ProjectMembership, User


class ScenarioImportExportTest(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="io", password="testpass123")
        self.project = Project.objects.create(name="IO Test", slug="io-test")
        ProjectMembership.objects.create(user=self.user, project=self.project, role=ProjectMembership.Role.AUDITOR)
        self.client.force_authenticate(user=self.user)
        self.pid = self.project.id

    def _create_scenario(self, key, title="T"):
        resp = self.client.post(
            f"/api/projects/{self.pid}/scenarios/create/",
            {"key": key, "title": title, "description": "desc", "expected_behavior": ["a"], "test_prompt": "p"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        return resp.json()

    def test_export_returns_scenarios(self):
        self._create_scenario("alpha", "Alpha")
        self._create_scenario("beta", "Beta")
        resp = self.client.get(f"/api/projects/{self.pid}/scenarios/export/")
        assert resp.status_code == 200, resp.content
        data = resp.json()
        self.assertEqual(data["count"], 2)
        keys = [s["key"] for s in data["scenarios"]]
        self.assertIn("alpha", keys)
        self.assertIn("beta", keys)
        # Check content fields present
        alpha = next(s for s in data["scenarios"] if s["key"] == "alpha")
        self.assertEqual(alpha["title"], "Alpha")
        self.assertEqual(alpha["description"], "desc")
        self.assertEqual(alpha["expected_behavior"], ["a"])
        self.assertEqual(alpha["test_prompt"], "p")
        self.assertEqual(data["format_version"], 2)
        self.assertEqual(alpha["metadata"], {})

    def test_agentic_metadata_round_trips_losslessly(self):
        metadata = {"agentic": {"schema_version": 1, "tools": {"forbidden": ["delete"]}}}
        resp = self.client.post(
            f"/api/projects/{self.pid}/scenarios/create/",
            {"key": "agentic", "title": "Agentic", "description": "d", "expected_behavior": ["x"], "metadata": metadata},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        export = self.client.get(f"/api/projects/{self.pid}/scenarios/export/").json()
        item = export["scenarios"][0]
        self.assertEqual(item["metadata"], metadata)
        self.assertEqual(item["severity_ceiling"], "")
        self.assertEqual(item["documents"], [])

        other = Project.objects.create(name="Target", slug="target")
        ProjectMembership.objects.create(user=self.user, project=other, role=ProjectMembership.Role.AUDITOR)
        imported = self.client.post(f"/api/projects/{other.id}/scenarios/import/", export, format="json")
        assert imported.status_code == 201, imported.content
        scenario = self.client.get(f"/api/projects/{other.id}/scenarios/").json()[0]
        self.assertEqual(scenario["latest_revision"]["metadata"], metadata)

    def test_metadata_only_edit_changes_revision_hash(self):
        original = self._create_scenario("hash-agentic")
        response = self.client.post(
            f"/api/projects/{self.pid}/scenarios/{original['id']}/update/",
            {"description": "desc", "expected_behavior": ["a"], "metadata": {"agentic": {"schema_version": 1}}},
            format="json",
        )
        assert response.status_code == 201, response.content
        self.assertEqual(response.json()["revision"], 2)
        self.assertNotEqual(response.json()["content_hash"], original["latest_revision"]["content_hash"])

    def test_export_empty_project(self):
        resp = self.client.get(f"/api/projects/{self.pid}/scenarios/export/")
        assert resp.status_code == 200
        self.assertEqual(resp.json()["count"], 0)

    def test_import_creates_new_scenarios(self):
        payload = {
            "scenarios": [
                {"key": "imp-1", "title": "Imp 1", "description": "d1", "expected_behavior": ["x"], "test_prompt": "p1"},
                {"key": "imp-2", "title": "Imp 2", "description": "d2", "expected_behavior": ["y"], "test_prompt": "p2"},
            ]
        }
        resp = self.client.post(f"/api/projects/{self.pid}/scenarios/import/", payload, format="json")
        assert resp.status_code == 201, resp.content
        body = resp.json()
        self.assertEqual(body["created"], 2)
        self.assertEqual(body["skipped"], 0)
        # Verify they exist
        resp = self.client.get(f"/api/projects/{self.pid}/scenarios/")
        keys = [s["key"] for s in resp.json()]
        self.assertIn("imp-1", keys)
        self.assertIn("imp-2", keys)

    def test_import_skips_existing_keys(self):
        self._create_scenario("existing")
        payload = {
            "scenarios": [
                {"key": "existing", "title": "Should Skip", "description": "d", "expected_behavior": [], "test_prompt": ""},
                {"key": "new-one", "title": "New", "description": "d", "expected_behavior": [], "test_prompt": ""},
            ]
        }
        resp = self.client.post(f"/api/projects/{self.pid}/scenarios/import/", payload, format="json")
        assert resp.status_code == 201, resp.content
        body = resp.json()
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["skipped"], 1)

    def test_import_bare_list(self):
        payload = [{"key": "bare", "title": "Bare", "description": "d", "expected_behavior": [], "test_prompt": ""}]
        resp = self.client.post(f"/api/projects/{self.pid}/scenarios/import/", payload, format="json")
        assert resp.status_code == 201, resp.content
        self.assertEqual(resp.json()["created"], 1)

    def test_import_invalid_format(self):
        resp = self.client.post(f"/api/projects/{self.pid}/scenarios/import/", {"foo": "bar"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_roundtrip_export_import(self):
        """Export from one project, import into another — full round-trip."""
        self._create_scenario("rt-1", "Round Trip")
        self._create_scenario("rt-2", "Second")
        # Export
        resp = self.client.get(f"/api/projects/{self.pid}/scenarios/export/")
        export_data = resp.json()
        # Create a second project
        user2 = User.objects.create_user(username="io2", password="testpass123")
        project2 = Project.objects.create(name="IO Target", slug="io-target")
        ProjectMembership.objects.create(user=user2, project=project2, role=ProjectMembership.Role.AUDITOR)
        self.client.force_authenticate(user=user2)
        # Import into project2
        resp = self.client.post(f"/api/projects/{project2.id}/scenarios/import/", export_data, format="json")
        assert resp.status_code == 201, resp.content
        self.assertEqual(resp.json()["created"], 2)
        # Verify
        resp = self.client.get(f"/api/projects/{project2.id}/scenarios/")
        keys = sorted(s["key"] for s in resp.json())
        self.assertEqual(keys, ["rt-1", "rt-2"])
