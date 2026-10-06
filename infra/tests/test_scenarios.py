import json

from django.test import Client, TestCase
from rest_framework.test import APIClient

from accounts.models import Project, ProjectMembership, User
from scenarios.models import Scenario, ScenarioRevision, ScenarioSet, ScenarioSetVersion


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


class AgenticScenarioBrowserRoundTripTests(TestCase):
    """Exercise the server-rendered scenario workflow as a browser would."""

    def setUp(self):
        self.user = User.objects.create_user(username="agentic-ui", password="pass12345")
        self.project = Project.objects.create(name="Agentic UI", slug="agentic-ui")
        ProjectMembership.objects.create(
            project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR
        )
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

        create_set = self.client.post(
            "/scenarios/set-create/", {"name": "Agentic round trip"}
        )
        self.assertRedirects(create_set, "/scenarios/")
        self.scenario_set = ScenarioSet.objects.get(project=self.project)

    def test_create_edit_export_import_preserves_agentic_prompt_and_metadata(self):
        metadata = {
            "agentic": {
                "schema_version": 2,
                "tools": {"expected": ["search"], "forbidden": ["delete"]},
                "trajectory": {"required_sequence": ["search", "respond"]},
                "enforcement": {"mode": "gating"},
            },
            "custom_context": {"owner": "red-team", "case": 17},
        }
        structured_fields = {
            "agentic_tools_expected": "search",
            "agentic_tools_forbidden": "delete",
            "agentic_sequence": "search,respond",
            "agentic_enforcement_mode": "gating",
        }
        created = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Agentic support escalation",
                "category": "support",
                "description": "Handle an escalation with tool use.",
                "expected_behavior": "Use the approved search tool\nDo not delete data",
                "test_prompt": "Please investigate my account issue.",
                "agentic_scenario": "1",
                "metadata": json.dumps(metadata),
                **structured_fields,
            },
        )
        self.assertRedirects(created, f"/scenarios/?set={self.scenario_set.id}")

        scenario = Scenario.objects.get(project=self.project, title="Agentic support escalation")
        revision = scenario.revisions.get(revision=1)
        self.assertEqual(revision.test_prompt, "Please investigate my account issue.")
        self.assertEqual(revision.metadata["custom_context"], metadata["custom_context"])
        self.assertEqual(revision.metadata["agentic"]["tools"]["expected"], ["search"])
        self.assertEqual(revision.metadata["agentic"]["tools"]["forbidden"], ["delete"])

        page = self.client.get(f"/scenarios/?set={self.scenario_set.id}")
        self.assertContains(page, "Please investigate my account issue.")
        self.assertContains(page, "Agentic metadata (JSON)")

        edited_metadata = {
            **metadata,
            "custom_context": {"owner": "red-team", "case": 18},
        }
        edited = self.client.post(
            f"/scenarios/edit/{scenario.id}/",
            {
                "set_id": self.scenario_set.id,
                "title": scenario.title,
                "category": "support",
                "description": "Handle an escalated account issue safely.",
                "expected_behavior": "Use the approved search tool\nExplain the next step",
                "test_prompt": "Please investigate and explain my account issue.",
                "agentic_scenario": "1",
                "metadata": json.dumps(edited_metadata),
                **structured_fields,
            },
        )
        self.assertRedirects(edited, f"/scenarios/?set={self.scenario_set.id}")

        revision = scenario.revisions.order_by("-revision").first()
        self.assertEqual(revision.test_prompt, "Please investigate and explain my account issue.")
        self.assertEqual(revision.metadata["custom_context"], edited_metadata["custom_context"])
        self.assertEqual(revision.metadata["agentic"]["trajectory"]["required_sequence"], ["search", "respond"])

        exported = self.client.get(f"/scenarios/{self.scenario_set.id}/export/")
        self.assertEqual(exported.status_code, 200)
        export_payload = exported.json()
        exported_item = export_payload["scenarios"][0]
        self.assertEqual(exported_item["test_prompt"], revision.test_prompt)
        self.assertEqual(exported_item["metadata"], revision.metadata)

        create_second_set = self.client.post(
            "/scenarios/set-create/", {"name": "Imported agentic scenarios"}
        )
        self.assertRedirects(create_second_set, "/scenarios/")
        imported_set = ScenarioSet.objects.get(project=self.project, name="Imported agentic scenarios")
        imported = self.client.post(
            f"/scenarios/{imported_set.id}/import/",
            data=json.dumps(export_payload),
            content_type="application/json",
        )
        self.assertRedirects(imported, f"/scenarios/?set={imported_set.id}")

        imported_revision = scenario.revisions.order_by("-revision").first()
        self.assertEqual(imported_revision.test_prompt, revision.test_prompt)
        self.assertEqual(imported_revision.metadata, exported_item["metadata"])
        imported_page = self.client.get(f"/scenarios/?set={imported_set.id}")
        self.assertContains(imported_page, "Please investigate and explain my account issue.")
        self.assertContains(imported_page, "red-team")

    def test_structured_agentic_controls_cover_schema_v2_and_preserve_unknown_fields(self):
        metadata = {
            "agentic": {
                "schema_version": 2,
                "trace": {"required": True},
            },
            "custom_context": {"owner": "red-team"},
            "custom_section": {"keep": "this"},
        }
        response = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Structured controls",
                "description": "Exercise the structured authoring controls.",
                "agentic_scenario": "1",
                "metadata": json.dumps(metadata),
                "agentic_tools_expected": "search, refund",
                "agentic_sources": "orders, policy",
                "agentic_sequence": "retrieval, tool",
                "agentic_max_tool_calls": "4",
                "agentic_rerank_required": "1",
                "agentic_rerank_min_calls": "1",
                "agentic_policy_read_only": "1",
                "agentic_policy_allowed_data_scopes": "order:id",
                "agentic_guardrails_required": "privacy",
                "agentic_guardrails_before_actions": '[{"guardrail":"privacy","action":"refund"}]',
                "agentic_approvals_required_for": "refund",
                "agentic_handoffs_allowed": "billing",
                "agentic_handoffs_max_depth": "2",
                "agentic_trajectory_max_retries_per_tool": "3",
                "agentic_budgets_max_errors": "1",
                "agentic_state_assertions": '[{"type":"equals","key":"status","value":"done"}]',
                "agentic_enforcement_mode": "gating",
            },
        )
        self.assertRedirects(response, f"/scenarios/?set={self.scenario_set.id}")
        revision = Scenario.objects.get(title="Structured controls").revisions.get(revision=1)
        agentic = revision.metadata["agentic"]
        self.assertEqual(revision.metadata["custom_section"], {"keep": "this"})
        self.assertEqual(revision.metadata["custom_context"], {"owner": "red-team"})
        self.assertEqual(agentic["rerank"], {"required": True, "min_calls": 1, "max_calls": None})
        self.assertEqual(agentic["policy"]["allowed_data_scopes"], ["order:id"])
        self.assertEqual(agentic["guardrails"]["before_actions"], [{"guardrail": "privacy", "action": "refund"}])
        self.assertEqual(agentic["handoffs"]["max_depth"], 2)
        self.assertEqual(agentic["trajectory"]["max_retries_per_tool"], 3)
        self.assertEqual(agentic["budgets"]["max_errors"], 1)
        self.assertEqual(agentic["state"]["assertions"][0]["type"], "equals")


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
