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
