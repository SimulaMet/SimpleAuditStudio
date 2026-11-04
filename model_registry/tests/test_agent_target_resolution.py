"""Regression tests for resolving an Agent to its Open WebUI wrapper model."""

from django.test import TestCase

from infra.tests.factories import AgentFactory
from model_registry.models import RegisteredModel
from model_registry.services import agent_target_model


class AgentTargetModelTest(TestCase):
    def test_resolves_synced_wrapper_not_base_model(self):
        agent = AgentFactory(external_id="studio.agent-target")

        target = agent_target_model(agent)

        self.assertIsInstance(target, RegisteredModel)
        self.assertEqual(target.model_id, agent.external_id)
        self.assertEqual(target.connection.name, "Open WebUI Agents")
        self.assertNotEqual(target.id, agent.base_model_id)

    def test_stale_owui_reference_is_repointed_to_base_model(self):
        """A managed Open WebUI Agents reference whose model_id no longer
        matches the agent's base model is reconciled on next resolution so
        execution hits a live endpoint instead of a stale one."""
        from model_registry.services import _ensure_agent_model_reference

        agent = AgentFactory(external_id="studio.agent-target")
        target = agent_target_model(agent)
        self.assertEqual(target.model_id, agent.external_id)

        # Simulate drift: the reference now points at a retired wrapper id.
        target.model_id = "stale.wrapper-id"
        target.save(update_fields=["model_id"])

        _ensure_agent_model_reference(agent, agent.created_by)
        target.refresh_from_db()
        # The managed reference is re-pointed at the agent's external_id so the
        # engine can resolve it against the live OpenWebUI endpoint.
        # (its primary key), which resolves to a live endpoint.
        self.assertEqual(target.model_id, agent.external_id)


class AgentReferencePerAgentTest(TestCase):
    def test_each_agent_keeps_its_own_reference_row(self):
        from infra.tests.factories import ProjectFactory

        project = ProjectFactory()
        a = AgentFactory(project=project, external_id="studio.a")
        b = AgentFactory(project=project, external_id="studio.b")
        ref_a = agent_target_model(a)
        ref_b = agent_target_model(b)
        self.assertNotEqual(ref_a.pk, ref_b.pk)
        ref_a.refresh_from_db()
        self.assertEqual(ref_a.model_id, "studio.a")
        self.assertEqual(ref_a.display_name, a.name)

    def test_deleted_agents_row_pinned_by_a_run_is_not_reused(self):
        from infra.tests.factories import AuditRunFactory, ProjectFactory

        project = ProjectFactory()
        a = AgentFactory(project=project, external_id="studio.a")
        ref_a = agent_target_model(a)
        AuditRunFactory(project=project, target_model=ref_a)
        a.delete()
        b = AgentFactory(project=project, external_id="studio.b")
        ref_b = agent_target_model(b)
        self.assertNotEqual(ref_a.pk, ref_b.pk)
        ref_a.refresh_from_db()
        self.assertEqual(ref_a.model_id, "studio.a")
