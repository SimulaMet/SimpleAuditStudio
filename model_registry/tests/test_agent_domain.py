"""Tests for the Agent configuration domain: models, serializers, API, and snapshots."""
import json

from django.db import IntegrityError
from django.test import Client, TestCase

from infra.tests.factories import (
    AgentFactory,
    KnowledgeBaseFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    RetrievalProfileFactory,
    ToolFactory,
    UserFactory,
)
from model_registry.models import (
    Agent,
    KnowledgeBase,
    RetrievalProfile,
    Tool,
)


class RetrievalProfileModelTest(TestCase):
    def test_create(self):
        project = ProjectFactory()
        profile = RetrievalProfile.objects.create(
            project=project, name="Default", search_mode="hybrid", top_k=8,
            rerank_enabled=True, rerank_top_k=4, relevance_threshold=0.4,
            bm25_weight=0.35, full_context=False,
        )
        self.assertEqual(profile.name, "Default")
        self.assertEqual(profile.search_mode, "hybrid")
        self.assertEqual(profile.top_k, 8)

    def test_config_dict(self):
        profile = RetrievalProfileFactory(
            search_mode="hybrid", top_k=10, rerank_enabled=True,
            rerank_top_k=5, relevance_threshold=0.5, bm25_weight=0.4,
        )
        d = profile.config_dict()
        self.assertEqual(d["search_mode"], "hybrid")
        self.assertEqual(d["top_k"], 10)
        self.assertTrue(d["rerank_enabled"])
        self.assertEqual(d["relevance_threshold"], 0.5)

    def test_unique_name_per_project(self):
        project = ProjectFactory()
        RetrievalProfile.objects.create(project=project, name="Dup")
        with self.assertRaises(IntegrityError):
            RetrievalProfile.objects.create(project=project, name="Dup")


class KnowledgeBaseModelTest(TestCase):
    def test_create(self):
        project = ProjectFactory()
        kb = KnowledgeBase.objects.create(
            project=project, name="HR Policies", external_id="owui-kb-1",
            authority="HR Dept", trust_level="high", sensitivity="confidential",
            version="2024.1",
        )
        self.assertEqual(kb.name, "HR Policies")
        self.assertEqual(kb.trust_level, "high")
        self.assertEqual(kb.sensitivity, "confidential")

    def test_str(self):
        kb = KnowledgeBaseFactory(name="My KB")
        self.assertEqual(str(kb), "My KB")


class ToolModelTest(TestCase):
    def test_create(self):
        project = ProjectFactory()
        tool = Tool.objects.create(
            project=project, name="Employee Lookup", type="builtin",
            read_only=True, external_network=False, handles_sensitive_data=True,
        )
        self.assertTrue(tool.read_only)
        self.assertFalse(tool.has_side_effects)
        self.assertTrue(tool.handles_sensitive_data)

    def test_side_effect_tool(self):
        project = ProjectFactory()
        tool = Tool.objects.create(
            project=project, name="Send Email", type="custom",
            read_only=False, has_side_effects=True, external_network=True,
        )
        self.assertFalse(tool.read_only)
        self.assertTrue(tool.has_side_effects)
        self.assertTrue(tool.external_network)


class AgentModelTest(TestCase):
    def test_create_agent(self):
        agent = AgentFactory()
        self.assertTrue(agent.name.startswith("Agent"))
        self.assertTrue(agent.enabled)
        self.assertEqual(agent.base_model.project, agent.project)

    def test_agent_references_existing_model(self):
        conn = ModelConnectionFactory()
        model = RegisteredModelFactory(connection=conn)
        agent = AgentFactory(base_model=model, project=conn.project)
        self.assertEqual(agent.base_model, model)

    def test_agent_attach_knowledge_base(self):
        agent = AgentFactory()
        kb = KnowledgeBaseFactory(project=agent.project)
        agent.knowledge_bases.add(kb)
        self.assertEqual(agent.knowledge_bases.count(), 1)

    def test_agent_attach_tool(self):
        agent = AgentFactory()
        tool = ToolFactory(project=agent.project)
        agent.tools.add(tool)
        self.assertEqual(agent.tools.count(), 1)

    def test_agent_retrieval_profile(self):
        agent = AgentFactory()
        profile = RetrievalProfileFactory(project=agent.project)
        agent.retrieval_profile = profile
        agent.save()
        self.assertEqual(agent.retrieval_profile, profile)

    def test_agent_permissions_persisted(self):
        agent = AgentFactory(capabilities={"knowledge_search": True, "code_execution": False})
        agent.refresh_from_db()
        self.assertTrue(agent.capabilities["knowledge_search"])
        self.assertFalse(agent.capabilities["code_execution"])

    def test_unique_name_per_project(self):
        project = ProjectFactory()
        AgentFactory(project=project, name="Dup")
        with self.assertRaises(IntegrityError):
            AgentFactory(project=project, name="Dup")

    def test_config_snapshot(self):
        agent = AgentFactory(
            name="Test Agent",
            system_prompt="Be helpful.",
            capabilities={"knowledge_search": True},
        )
        kb = KnowledgeBaseFactory(project=agent.project, name="KB1", external_id="owui-1", version="v1")
        tool = ToolFactory(project=agent.project, name="Calc", type="builtin")
        profile = RetrievalProfileFactory(project=agent.project, name="Hybrid", search_mode="hybrid", top_k=8)

        agent.knowledge_bases.add(kb)
        agent.tools.add(tool)
        agent.retrieval_profile = profile
        agent.save()

        snap = agent.config_snapshot()
        self.assertEqual(snap["name"], "Test Agent")
        self.assertEqual(snap["system_prompt"], "Be helpful.")
        self.assertEqual(snap["base_model"]["model_id"], agent.base_model.model_id)
        self.assertEqual(len(snap["knowledge_bases"]), 1)
        self.assertEqual(snap["knowledge_bases"][0][1], "KB1")
        self.assertEqual(len(snap["tools"]), 1)
        self.assertEqual(snap["tools"][0][1], "Calc")
        self.assertEqual(snap["retrieval_profile"]["name"], "Hybrid")
        self.assertEqual(snap["retrieval_profile"]["top_k"], 8)
        self.assertTrue(snap["capabilities"]["knowledge_search"])

    def test_snapshot_without_profile(self):
        agent = AgentFactory(retrieval_profile=None)
        snap = agent.config_snapshot()
        self.assertIsNone(snap["retrieval_profile"])

    def test_deleting_shared_resources_does_not_invalidate_snapshot(self):
        """A config snapshot is a plain dict — deleting live objects doesn't change it."""
        agent = AgentFactory()
        kb = KnowledgeBaseFactory(project=agent.project)
        agent.knowledge_bases.add(kb)
        snap = agent.config_snapshot()
        kb.delete()
        # The snapshot is already captured; it still references the KB by name.
        self.assertEqual(len(snap["knowledge_bases"]), 1)


class AgentAPITest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.force_login(self.user)
        # Set the active project in session (ProjectMiddleware reads this)
        self.client.session["active_project_id"] = self.project.id
        self.session = self.client.session
        self.session.save()

    def _api(self, path, method="get", data=None):
        if method == "get":
            return self.client.get(path)
        elif method == "post":
            return self.client.post(path, data=json.dumps(data), content_type="application/json")
        elif method == "put":
            return self.client.put(path, data=json.dumps(data), content_type="application/json")
        elif method == "delete":
            return self.client.delete(path)

    def test_list_agents_empty(self):
        resp = self._api("/api/agents/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_create_agent(self):
        model = RegisteredModelFactory(project=self.project)
        resp = self._api("/api/agents/", "post", {
            "name": "My Agent",
            "description": "Test agent",
            "base_model": model.id,
            "system_prompt": "Be helpful.",
            "capabilities": {"knowledge_search": True},
        })
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["name"], "My Agent")
        self.assertEqual(data["base_model"], model.id)
        self.assertTrue(Agent.objects.filter(name="My Agent", project=self.project).exists())

    def test_create_agent_requires_name(self):
        model = RegisteredModelFactory(project=self.project)
        resp = self._api("/api/agents/", "post", {
            "base_model": model.id,
        })
        self.assertEqual(resp.status_code, 400)

    def test_get_agent_detail(self):
        agent = AgentFactory(project=self.project)
        resp = self._api(f"/api/agents/{agent.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], agent.name)

    def test_update_agent(self):
        agent = AgentFactory(project=self.project)
        resp = self._api(f"/api/agents/{agent.id}/", "put", {
            "name": "Updated",
            "description": "New desc",
            "base_model": agent.base_model.id,
            "system_prompt": "Updated prompt.",
            "capabilities": {},
            "metadata": {},
            "enabled": True,
        })
        self.assertEqual(resp.status_code, 200)
        agent.refresh_from_db()
        self.assertEqual(agent.name, "Updated")

    def test_delete_agent(self):
        agent = AgentFactory(project=self.project)
        resp = self._api(f"/api/agents/{agent.id}/", "delete")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Agent.objects.filter(pk=agent.id).exists())

    def test_agent_snapshot_endpoint(self):
        agent = AgentFactory(project=self.project)
        resp = self._api(f"/api/agents/{agent.id}/snapshot/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["name"], agent.name)
        self.assertIn("base_model", data)
        self.assertIn("capabilities", data)

    def test_cross_project_model_rejected(self):
        other_project = ProjectFactory()
        other_model = RegisteredModelFactory(project=other_project)
        resp = self._api("/api/agents/", "post", {
            "name": "Bad Agent",
            "base_model": other_model.id,
        })
        self.assertEqual(resp.status_code, 400)

    def test_agent_with_knowledge_base(self):
        model = RegisteredModelFactory(project=self.project)
        kb = KnowledgeBaseFactory(project=self.project)
        resp = self._api("/api/agents/", "post", {
            "name": "KB Agent",
            "base_model": model.id,
            "knowledge_bases": [kb.id],
        })
        self.assertEqual(resp.status_code, 201)
        agent = Agent.objects.get(name="KB Agent")
        self.assertIn(kb, agent.knowledge_bases.all())

    def test_agent_with_tools(self):
        model = RegisteredModelFactory(project=self.project)
        tool = ToolFactory(project=self.project)
        resp = self._api("/api/agents/", "post", {
            "name": "Tool Agent",
            "base_model": model.id,
            "tools": [tool.id],
        })
        self.assertEqual(resp.status_code, 201)
        agent = Agent.objects.get(name="Tool Agent")
        self.assertIn(tool, agent.tools.all())

class RetrievalProfileAPITest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.force_login(self.user)
        self.client.session["active_project_id"] = self.project.id
        self.session = self.client.session
        self.session.save()

    def test_create_profile(self):
        resp = self.client.post(
            "/api/retrieval-profiles/",
            data=json.dumps({"name": "Fast", "search_mode": "semantic", "top_k": 3}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["name"], "Fast")

    def test_list_profiles(self):
        RetrievalProfileFactory(project=self.project, name="P1")
        RetrievalProfileFactory(project=self.project, name="P2")
        resp = self.client.get("/api/retrieval-profiles/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 2)

    def test_delete_profile(self):
        profile = RetrievalProfileFactory(project=self.project)
        resp = self.client.delete(f"/api/retrieval-profiles/{profile.id}/")
        self.assertEqual(resp.status_code, 204)


class KnowledgeBaseAPITest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.force_login(self.user)
        self.client.session["active_project_id"] = self.project.id
        self.session = self.client.session
        self.session.save()

    def test_create_kb(self):
        resp = self.client.post(
            "/api/knowledge-bases/",
            data=json.dumps({
                "name": "HR Policies",
                "external_id": "owui-kb-1",
                "trust_level": "high",
                "sensitivity": "confidential",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["name"], "HR Policies")

    def test_list_kbs(self):
        KnowledgeBaseFactory(project=self.project)
        resp = self.client.get("/api/knowledge-bases/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)


class ToolAPITest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.force_login(self.user)
        self.client.session["active_project_id"] = self.project.id
        self.session = self.client.session
        self.session.save()

    def test_create_tool(self):
        resp = self.client.post(
            "/api/tools/",
            data=json.dumps({
                "name": "Calculator",
                "type": "builtin",
                "read_only": True,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["name"], "Calculator")

    def test_list_tools(self):
        ToolFactory(project=self.project)
        resp = self.client.get("/api/tools/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)


class AgentUITest(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.force_login(self.user)
        self.client.session["active_project_id"] = self.project.id
        self.session = self.client.session
        self.session.save()

    def test_agents_list_page(self):
        resp = self.client.get("/agents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Agents")

    def test_agent_new_page(self):
        resp = self.client.get("/agents/new/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "New Agent")

    def test_agent_create_via_ui(self):
        model = RegisteredModelFactory(project=self.project)
        resp = self.client.post("/agents/new/", {
            "name": "UI Agent",
            "description": "Created via UI",
            "base_model": model.id,
            "system_prompt": "Test prompt",
            "enabled": "on",
            "knowledge_search": "on",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Agent.objects.filter(name="UI Agent", project=self.project).exists())

    def test_agent_detail_page(self):
        agent = AgentFactory(project=self.project)
        resp = self.client.get(f"/agents/{agent.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, agent.name)

    def test_agent_delete_via_ui(self):
        agent = AgentFactory(project=self.project)
        resp = self.client.post(f"/agents/{agent.id}/delete/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Agent.objects.filter(pk=agent.id).exists())


# ---------------------------------------------------------------------------
# Phase 10: Agent as Audit Target
# ---------------------------------------------------------------------------


class AgentAuditTargetTest(TestCase):
    """Agent can be selected as an audit target; its config is frozen on the run."""

    def setUp(self):
        self.user = UserFactory()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.force_login(self.user)
        self.client.session["active_project_id"] = self.project.id
        self.session = self.client.session
        self.session.save()

        # Build the full agent stack
        self.agent = AgentFactory(project=self.project)
        self.kb = KnowledgeBaseFactory(project=self.project)
        self.tool = ToolFactory(project=self.project)
        self.agent.knowledge_bases.add(self.kb)
        self.agent.tools.add(self.tool)
        self.agent.save()

    def _create_audit_run(self, **overrides):
        """Create an audit run via the service layer with an agent target."""
        from audits.services import create_audit_run
        from infra.tests.factories import (
            JudgeFactory,
            JudgeVersionFactory,
            ScenarioSetFactory,
            ScenarioSetVersionFactory,
        )

        scenario_set = ScenarioSetFactory(project=self.project)
        version = ScenarioSetVersionFactory(scenario_set=scenario_set)
        judge = JudgeFactory(project=self.project)
        judge_version = JudgeVersionFactory(judge=judge)
        auditor_model = RegisteredModelFactory(project=self.project)
        judge_model = RegisteredModelFactory(project=self.project)

        kwargs = {
            "project": self.project,
            "user": self.user,
            "name": "Agent Audit Run",
            "scenario_set_version": version,
            "target_model": self.agent.base_model,
            "auditor_model": auditor_model,
            "judge_model": judge_model,
            "judge": judge_version,
            "agent": self.agent,
        }
        kwargs.update(overrides)
        return create_audit_run(**kwargs)

    def test_agent_as_audit_target(self):
        run = self._create_audit_run()
        self.assertEqual(run.agent_id, self.agent.id)
        self.assertIsNotNone(run.agent_config_snapshot)
        self.assertEqual(run.agent_config_snapshot["agent_id"], self.agent.id)
        self.assertEqual(run.agent_config_snapshot["name"], self.agent.name)

    def test_agent_snapshot_frozen_at_creation(self):
        run = self._create_audit_run()
        snapshot = run.agent_config_snapshot

        # Edit the agent after the run was created
        self.agent.system_prompt = "Changed after run"
        self.agent.name = "Renamed Agent"
        self.agent.save()

        # The run's snapshot must be unchanged
        run.refresh_from_db()
        self.assertEqual(run.agent_config_snapshot["system_prompt"], snapshot["system_prompt"])
        self.assertEqual(run.agent_config_snapshot["name"], snapshot["name"])
        self.assertNotEqual(run.agent_config_snapshot["name"], "Renamed Agent")

    def test_agent_snapshot_includes_knowledge_bases(self):
        run = self._create_audit_run()
        kbs = run.agent_config_snapshot["knowledge_bases"]
        self.assertEqual(len(kbs), 1)
        # values_list("id", "name", "external_id", "version") → [id, name, external_id, version]
        self.assertEqual(kbs[0][1], self.kb.name)
        self.assertEqual(kbs[0][2], self.kb.external_id)

    def test_agent_snapshot_includes_tools(self):
        run = self._create_audit_run()
        tools = run.agent_config_snapshot["tools"]
        self.assertEqual(len(tools), 1)
        # values_list("id", "name", "type") → [id, name, type]
        self.assertEqual(tools[0][1], self.tool.name)

    def test_agent_snapshot_includes_base_model(self):
        run = self._create_audit_run()
        bm = run.agent_config_snapshot["base_model"]
        self.assertEqual(bm["id"], self.agent.base_model.id)
        self.assertEqual(bm["model_id"], self.agent.base_model.model_id)

    def test_agent_snapshot_includes_retrieval_profile(self):
        profile = RetrievalProfileFactory(project=self.project)
        self.agent.retrieval_profile = profile
        self.agent.save()

        run = self._create_audit_run()
        rp = run.agent_config_snapshot["retrieval_profile"]
        self.assertIsNotNone(rp)
        self.assertEqual(rp["id"], profile.id)
        self.assertEqual(rp["top_k"], profile.top_k)

    def test_agent_snapshot_includes_capabilities(self):
        self.agent.capabilities = ["rag", "tools"]
        self.agent.save()

        run = self._create_audit_run()
        self.assertEqual(run.agent_config_snapshot["capabilities"], ["rag", "tools"])

    def test_agent_snapshot_includes_metadata(self):
        self.agent.metadata = {"team": "audit", "version": "2.0"}
        self.agent.save()

        run = self._create_audit_run()
        self.assertEqual(run.agent_config_snapshot["metadata"], {"team": "audit", "version": "2.0"})

    def test_agent_cross_project_rejected(self):
        other_project = ProjectFactory()
        other_agent = AgentFactory(project=other_project)

        from infra.exceptions import StableAPIError

        with self.assertRaises(StableAPIError) as ctx:
            self._create_audit_run(agent=other_agent)
        self.assertEqual(ctx.exception.code, "cross_project_input")

    def test_agent_disabled_rejected(self):
        self.agent.enabled = False
        self.agent.save()

        from infra.exceptions import StableAPIError

        with self.assertRaises(StableAPIError) as ctx:
            self._create_audit_run()
        self.assertEqual(ctx.exception.code, "agent_disabled")

    def test_agent_model_mismatch_rejected(self):
        mismatched_model = RegisteredModelFactory(project=self.project)

        from infra.exceptions import StableAPIError

        with self.assertRaises(StableAPIError) as ctx:
            self._create_audit_run(target_model=mismatched_model)
        self.assertEqual(ctx.exception.code, "agent_model_mismatch")

    def test_no_agent_run_has_null_snapshot(self):
        from audits.services import create_audit_run
        from infra.tests.factories import (
            JudgeFactory,
            JudgeVersionFactory,
            ScenarioSetFactory,
            ScenarioSetVersionFactory,
        )

        scenario_set = ScenarioSetFactory(project=self.project)
        version = ScenarioSetVersionFactory(scenario_set=scenario_set)
        judge = JudgeFactory(project=self.project)
        judge_version = JudgeVersionFactory(judge=judge)
        auditor_model = RegisteredModelFactory(project=self.project)
        judge_model = RegisteredModelFactory(project=self.project)

        run = create_audit_run(
            project=self.project,
            user=self.user,
            name="Bare Model Run",
            scenario_set_version=version,
            target_model=self.agent.base_model,
            auditor_model=auditor_model,
            judge_model=judge_model,
            judge=judge_version,
        )
        self.assertIsNone(run.agent)
        self.assertIsNone(run.agent_config_snapshot)

    def test_agent_deletion_preserves_run_snapshot(self):
        """Deleting the agent does not invalidate the historical run's snapshot."""
        run = self._create_audit_run()
        snapshot = json.loads(json.dumps(run.agent_config_snapshot))

        # Agent FK is SET_NULL, so the run survives
        self.agent.delete()
        run.refresh_from_db()
        self.assertIsNone(run.agent)
        # But the frozen snapshot is intact (compare via JSON to normalize tuples→lists)
        self.assertEqual(json.loads(json.dumps(run.agent_config_snapshot)), snapshot)

    def test_agent_api_create_run_with_agent(self):
        """The API accepts agent_id and freezes the agent config."""
        from infra.tests.factories import (
            JudgeFactory,
            JudgeVersionFactory,
            ScenarioSetFactory,
            ScenarioSetVersionFactory,
        )

        scenario_set = ScenarioSetFactory(project=self.project)
        version = ScenarioSetVersionFactory(scenario_set=scenario_set)
        judge = JudgeFactory(project=self.project)
        judge_version = JudgeVersionFactory(judge=judge)
        auditor_model = RegisteredModelFactory(project=self.project)
        judge_model = RegisteredModelFactory(project=self.project)

        resp = self.client.post(
            f"/api/projects/{self.project.id}/audit-runs/create/",
            data=json.dumps({
                "name": "API Agent Run",
                "scenario_set_version_id": version.id,
                "target_model_id": self.agent.base_model.id,
                "agent_id": self.agent.id,
                "auditor_model_id": auditor_model.id,
                "judge_model_id": judge_model.id,
                "judge_version_id": judge_version.id,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201, resp.content[:500])
        data = resp.json()
        self.assertEqual(data["agent"], self.agent.id)
        self.assertIsNotNone(data["agent_config_snapshot"])
        self.assertEqual(data["agent_config_snapshot"]["name"], self.agent.name)

    def test_agent_api_create_run_cross_project_agent(self):
        """The API rejects an agent from another project."""
        from infra.tests.factories import (
            JudgeFactory,
            JudgeVersionFactory,
            ScenarioSetFactory,
            ScenarioSetVersionFactory,
        )

        other_project = ProjectFactory()
        other_agent = AgentFactory(project=other_project)
        scenario_set = ScenarioSetFactory(project=self.project)
        version = ScenarioSetVersionFactory(scenario_set=scenario_set)
        judge = JudgeFactory(project=self.project)
        judge_version = JudgeVersionFactory(judge=judge)
        auditor_model = RegisteredModelFactory(project=self.project)
        judge_model = RegisteredModelFactory(project=self.project)

        resp = self.client.post(
            f"/api/projects/{self.project.id}/audit-runs/create/",
            data=json.dumps({
                "name": "Cross Project Agent Run",
                "scenario_set_version_id": version.id,
                "target_model_id": self.agent.base_model.id,
                "agent_id": other_agent.id,
                "auditor_model_id": auditor_model.id,
                "judge_model_id": judge_model.id,
                "judge_version_id": judge_version.id,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_frozen_agent_helper(self):
        """The frozen_agent() service returns the snapshot data."""
        from audits.services import frozen_agent

        run = self._create_audit_run()
        fa = frozen_agent(run)
        self.assertIsNotNone(fa)
        self.assertEqual(fa["name"], self.agent.name)
        self.assertEqual(fa["id"], self.agent.id)
        self.assertEqual(len(fa["knowledge_bases"]), 1)
        self.assertEqual(len(fa["tools"]), 1)

    def test_frozen_agent_none_for_bare_model_run(self):
        from audits.services import create_audit_run, frozen_agent
        from infra.tests.factories import (
            JudgeFactory,
            JudgeVersionFactory,
            ScenarioSetFactory,
            ScenarioSetVersionFactory,
        )

        scenario_set = ScenarioSetFactory(project=self.project)
        version = ScenarioSetVersionFactory(scenario_set=scenario_set)
        judge = JudgeFactory(project=self.project)
        judge_version = JudgeVersionFactory(judge=judge)
        auditor_model = RegisteredModelFactory(project=self.project)
        judge_model = RegisteredModelFactory(project=self.project)

        run = create_audit_run(
            project=self.project,
            user=self.user,
            name="No Agent",
            scenario_set_version=version,
            target_model=self.agent.base_model,
            auditor_model=auditor_model,
            judge_model=judge_model,
            judge=judge_version,
        )
        self.assertIsNone(frozen_agent(run))
