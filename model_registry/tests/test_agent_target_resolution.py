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
