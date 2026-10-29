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

    def test_sparse_create_stores_only_active_sections(self):
        """A tool-only scenario must not materialize dormant sections."""
        response = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Sparse tool check",
                "description": "Only the tools section is active.",
                "agentic_scenario": "1",
                "agentic_tools_expected": "acme_get_order",
                "agentic_enforcement_mode": "gating",
            },
        )
        self.assertRedirects(response, f"/scenarios/?set={self.scenario_set.id}")
        revision = Scenario.objects.get(title="Sparse tool check").revisions.get(revision=1)
        agentic = revision.metadata["agentic"]
        self.assertEqual(agentic["tools"]["expected"], [
            {"name": "acme_get_order", "min_calls": 1, "max_calls": None},
        ])
        # Dormant list sections never materialize; checkbox sentinels record
        # explicit false/0 assertions only (no criteria, no scopes, no budgets).
        for section in ("retrieval", "trajectory", "budgets", "handoffs",
                        "guardrails", "state", "trace", "goal"):
            self.assertNotIn(section, agentic)
        # Checkbox sentinels record explicit false assertions only.
        self.assertEqual(agentic["rerank"], {"required": False})
        self.assertEqual(agentic["policy"], {"read_only": False, "allow_side_effects": False})
        self.assertEqual(agentic["approvals"], {"must_precede_execution": False})
        self.assertEqual(agentic["semantic_judge"], {"enabled": False})
        # The listing renders the read-only summary with the active tool only;
        # dormant-section labels (e.g. forbidden data scopes) never appear.
        page = self.client.get(f"/scenarios/?set={self.scenario_set.id}")
        self.assertContains(page, "acme_get_order")
        self.assertNotContains(page, "Forbidden data scopes:")
        self.assertNotContains(page, "Required sources:")


    def _valid_decision(self):
        return {
            "instructions": "Did the court find the defendant guilty?",
            "criteria": {"yes": "Found guilty", "no": "Not found guilty"},
            "accepted": ["no"],
        }

    def test_create_decision_scenario_stores_block_and_renders_page(self):
        response = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Guilty verdict",
                "description": "Classify the verdict.",
                "decision_model_scenario": "1",
                "decision": json.dumps(self._valid_decision()),
            },
        )
        self.assertRedirects(response, f"/scenarios/?set={self.scenario_set.id}")
        revision = Scenario.objects.get(title="Guilty verdict").revisions.get(revision=1)
        self.assertEqual(revision.decision, self._valid_decision())
        # The page renders the checkbox checked and the read-only decision value.
        page = self.client.get(f"/scenarios/?set={self.scenario_set.id}")
        self.assertContains(page, 'name="decision_model_scenario"')
        self.assertContains(page, "Found guilty")

    def test_edit_decision_scenario_saves_and_clears(self):
        created = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Verdict edit",
                "description": "d",
                "decision_model_scenario": "1",
                "decision": json.dumps(self._valid_decision()),
            },
        )
        self.assertRedirects(created, f"/scenarios/?set={self.scenario_set.id}")
        scenario = Scenario.objects.get(title="Verdict edit")

        edited = self.client.post(
            f"/scenarios/edit/{scenario.id}/",
            {
                "set_id": self.scenario_set.id,
                "title": scenario.title,
                "category": "",
                "description": "d",
                "expected_behavior": "",
                "test_prompt": "",
                "severity_ceiling": "",
                "documents": "",
                "file_uri": "",
                "decision_model_scenario": "1",
                "decision": json.dumps(self._valid_decision()),
            },
        )
        self.assertRedirects(edited, f"/scenarios/?set={self.scenario_set.id}")
        latest = scenario.revisions.order_by("-revision").first()
        self.assertEqual(latest.decision, self._valid_decision())

        # Unchecking + emptying clears the block (None is passed explicitly).
        cleared = self.client.post(
            f"/scenarios/edit/{scenario.id}/",
            {
                "set_id": self.scenario_set.id,
                "title": scenario.title,
                "category": "",
                "description": "d",
                "expected_behavior": "",
                "test_prompt": "",
                "severity_ceiling": "",
                "documents": "",
                "file_uri": "",
                "decision_model_scenario": "",
                "decision": "",
            },
        )
        self.assertRedirects(cleared, f"/scenarios/?set={self.scenario_set.id}")
        self.assertIsNone(scenario.revisions.order_by("-revision").first().decision)

    def test_create_decision_scenario_rejects_invalid_json(self):
        response = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Bad decision",
                "description": "d",
                "decision_model_scenario": "1",
                "decision": "{not valid json",
            },
        )
        self.assertRedirects(response, f"/scenarios/?set={self.scenario_set.id}")
        self.assertFalse(Scenario.objects.filter(title="Bad decision").exists())

    def test_cannot_be_both_agentic_and_decision(self):
        response = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Both modes",
                "description": "d",
                "agentic_scenario": "1",
                "agentic_tools_expected": "acme_get_order",
                "decision_model_scenario": "1",
                "decision": json.dumps(self._valid_decision()),
            },
        )
        self.assertRedirects(response, f"/scenarios/?set={self.scenario_set.id}")
        self.assertFalse(Scenario.objects.filter(title="Both modes").exists())

    def test_create_decision_scenario_rejects_malformed_block(self):
        # Valid JSON but missing criteria — the engine schema rejects it.
        bad = {"instructions": "Q?", "criteria": {"only": "one option"}}
        response = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Malformed decision",
                "description": "d",
                "decision_model_scenario": "1",
                "decision": json.dumps(bad),
            },
        )
        self.assertRedirects(response, f"/scenarios/?set={self.scenario_set.id}")
        self.assertFalse(Scenario.objects.filter(title="Malformed decision").exists())

    def test_clearing_a_section_drops_it_from_saved_metadata(self):
        """Clearing all fields of an active section removes the assertion."""
        self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Section clearing",
                "description": "Tools are active, then cleared.",
                "agentic_scenario": "1",
                "agentic_tools_forbidden": "acme_create_refund_request",
            },
        )
        scenario = Scenario.objects.get(title="Section clearing")
        first = scenario.revisions.get(revision=1)
        self.assertEqual(first.metadata["agentic"]["tools"], {"forbidden": ["acme_create_refund_request"]})

        # The browser submits every rendered control; empty values clear the
        # assertion, checkbox sentinels carry explicit false.
        edited = self.client.post(
            f"/scenarios/edit/{scenario.id}/",
            {
                "set_id": self.scenario_set.id,
                "title": scenario.title,
                "category": "",
                "description": "Tools are active, then cleared.",
                "expected_behavior": "",
                "test_prompt": "",
                "severity_ceiling": "",
                "documents": "",
                "file_uri": "",
                "agentic_scenario": "1",
                "agentic_tools_expected": "",
                "agentic_tools_forbidden": "",
                "agentic_tools_allowed": "",
                "agentic_sources": "",
                "agentic_retrieval_min_documents": "",
                "agentic_retrieval_max_documents": "",
                "agentic_rerank_required": "0",
                "agentic_rerank_min_calls": "",
                "agentic_rerank_max_calls": "",
                "agentic_policy_read_only": "0",
                "agentic_policy_allowed_data_scopes": "",
                "agentic_policy_forbidden_data_scopes": "",
                "agentic_policy_forbidden_side_effects": "",
                "agentic_policy_allowed_destinations": "",
                "agentic_policy_allow_side_effects": "0",
                "agentic_guardrails_required": "",
                "agentic_guardrails_must_pass": "",
                "agentic_guardrails_before_actions": "",
                "agentic_approvals_required_for": "",
                "agentic_approvals_must_precede_execution": "0",
                "agentic_handoffs_allowed": "",
                "agentic_handoffs_forbidden": "",
                "agentic_handoffs_required": "",
                "agentic_handoffs_max_handoffs": "",
                "agentic_handoffs_max_depth": "",
                "agentic_sequence": "",
                "agentic_trajectory_max_steps": "",
                "agentic_trajectory_max_retries_per_tool": "",
                "agentic_trajectory_max_identical_consecutive_calls": "",
                "agentic_max_tool_calls": "",
                "agentic_budgets_max_errors": "",
                "agentic_state_assertions": "",
                "agentic_semantic_judge_enabled": "0",
                "agentic_semantic_judge_criteria": "",
            },
        )
        self.assertRedirects(edited, f"/scenarios/?set={self.scenario_set.id}")
        latest = scenario.revisions.order_by("-revision").first()
        self.assertNotIn("tools", latest.metadata["agentic"])


    def test_create_edit_export_import_preserves_agentic_prompt_and_metadata(self):
        # The raw metadata box is a read-only preview; the structured controls
        # are the single source of truth. Unknown top-level fields cannot be
        # authored in the UI anymore, but any that exist (e.g. from an import or
        # older client) survive an edit because they are carried over from the
        # previous revision rather than re-read from the textarea.
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
                **structured_fields,
            },
        )
        self.assertRedirects(created, f"/scenarios/?set={self.scenario_set.id}")

        scenario = Scenario.objects.get(project=self.project, title="Agentic support escalation")
        revision = scenario.revisions.get(revision=1)
        self.assertEqual(revision.test_prompt, "Please investigate my account issue.")
        # Expected tools are stored as rich specs ({name, min_calls, max_calls});
        # the form authors them by name, which wraps into this shape.
        self.assertEqual(revision.metadata["agentic"]["tools"]["expected"], [{"name": "search", "min_calls": 1, "max_calls": None}])
        self.assertEqual(revision.metadata["agentic"]["tools"]["forbidden"], ["delete"])

        page = self.client.get(f"/scenarios/?set={self.scenario_set.id}")
        self.assertContains(page, "Please investigate my account issue.")
        self.assertContains(page, "Agentic metadata preview (read-only)")

        # Seed an unknown field into the stored revision directly — simulating
        # metadata that arrived via import or an older client, which the UI no
        # longer exposes for editing but must still preserve across edits.
        revision.metadata["custom_context"] = {"owner": "red-team", "case": 17}
        revision.save(update_fields=["metadata"])

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
                **structured_fields,
            },
        )
        self.assertRedirects(edited, f"/scenarios/?set={self.scenario_set.id}")

        revision = scenario.revisions.order_by("-revision").first()
        self.assertEqual(revision.test_prompt, "Please investigate and explain my account issue.")
        self.assertEqual(revision.metadata["custom_context"], {"owner": "red-team", "case": 17})
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
        # The raw metadata box is a read-only preview; the structured controls
        # are the single source of truth. Unknown top-level fields can't be
        # authored in the UI anymore, but any that exist (e.g. from an import)
        # survive an edit because they are carried over from the previous
        # revision rather than re-read from the textarea.
        response = self.client.post(
            "/scenarios/create/",
            {
                "set_id": self.scenario_set.id,
                "name": "Structured controls",
                "description": "Exercise the structured authoring controls.",
                "agentic_scenario": "1",
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
        scenario = Scenario.objects.get(title="Structured controls")
        revision = scenario.revisions.get(revision=1)
        agentic = revision.metadata["agentic"]
        # A fresh create has no prior revision, so there are no unknown fields to preserve.
        self.assertNotIn("custom_section", revision.metadata)
        self.assertNotIn("custom_context", revision.metadata)
        # Simulate an import / older client that stored an unknown field, then
        # confirm a structured edit carries it forward (not dropped).
        revision.metadata["custom_section"] = {"keep": "this"}
        revision.metadata["custom_context"] = {"owner": "red-team"}
        revision.save(update_fields=["metadata"])
        edited = self.client.post(
            f"/scenarios/edit/{scenario.id}/",
            {
                "set_id": self.scenario_set.id,
                "title": scenario.title,
                "category": "",
                "description": "Exercise the structured authoring controls.",
                "expected_behavior": "",
                "test_prompt": "",
                "severity_ceiling": "",
                "documents": "",
                "file_uri": "",
                "agentic_scenario": "1",
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
        self.assertRedirects(edited, f"/scenarios/?set={self.scenario_set.id}")
        revision = scenario.revisions.order_by("-revision").first()
        self.assertEqual(revision.metadata["custom_section"], {"keep": "this"})
        self.assertEqual(revision.metadata["custom_context"], {"owner": "red-team"})
        agentic = revision.metadata["agentic"]
        self.assertEqual(agentic["tools"]["expected"], [{"name": "search", "min_calls": 1, "max_calls": None}, {"name": "refund", "min_calls": 1, "max_calls": None}])
        # Sparse semantics: sections whose form fields were left empty are not
        # stored at all (absent = not checked), and no null-valued keys leak in.
        self.assertEqual(agentic["rerank"], {"required": True, "min_calls": 1})
        self.assertNotIn("max_calls", agentic["rerank"])
        self.assertEqual(agentic["policy"]["allowed_data_scopes"], ["order:id"])
        self.assertNotIn("forbidden_data_scopes", agentic["policy"])
        self.assertEqual(agentic["guardrails"]["before_actions"], [{"guardrail": "privacy", "action": "refund"}])
        self.assertEqual(agentic["handoffs"]["max_depth"], 2)
        self.assertNotIn("forbidden", agentic["handoffs"])
        self.assertEqual(agentic["trajectory"]["max_retries_per_tool"], 3)
        self.assertNotIn("max_steps", agentic["trajectory"])
        self.assertEqual(agentic["budgets"]["max_errors"], 1)
        self.assertEqual(agentic["budgets"]["max_tool_calls"], 4)
        self.assertEqual(agentic["state"]["assertions"][0]["type"], "equals")
        # Dormant sections never materialize; semantic_judge appears only as an
        # explicit disabled assertion (checkbox sentinel), not with criteria.
        for section in ("trace", "goal"):
            self.assertNotIn(section, agentic)
        self.assertEqual(agentic.get("semantic_judge"), {"enabled": False})


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
