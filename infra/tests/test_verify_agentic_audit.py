from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase


class VerifyAgenticAuditTests(TestCase):
    def test_verification_fixture_covers_multi_agent_parentage(self):
        from infra.management.commands.verify_agentic_audit import _fixture_checks

        self.assertTrue(_fixture_checks())

    def test_verification_command_executes_fixture_and_evaluator_checks(self):
        output = StringIO()

        with (
            patch("model_registry.models.ModelConnection.objects.filter") as connections,
            patch("model_registry.models.Agent.objects.filter") as agents,
        ):
            connections.return_value.exists.return_value = True
            agents.return_value.exists.return_value = True
            call_command("verify_agentic_audit", stdout=output)

        rendered = output.getvalue()
        self.assertIn("PASS\tschema-v2-validation", rendered)
        self.assertIn("PASS\tdeterministic-tool-check", rendered)
        self.assertIn("PASS\ttrace-fixture", output.getvalue())
        self.assertIn("PASS\totlp-correlation", rendered)
        self.assertIn("acceptance checks passed", rendered)

    def test_executable_checks_reject_schema_and_preserve_structural_privacy(self):
        from infra.management.commands.verify_agentic_audit import (
            _privacy_check,
            _schema_check,
        )

        self.assertEqual(_schema_check()[0], "schema-v2-validation")
        self.assertTrue(_schema_check()[1])
        self.assertEqual(_privacy_check()[0], "privacy-no-key-in-source")
        self.assertTrue(_privacy_check()[1])

    def test_local_runtime_harness_checks_correlation_and_judge_order(self):
        output = StringIO()
        with (
            patch("model_registry.models.ModelConnection.objects.filter") as connections,
            patch("model_registry.models.Agent.objects.filter") as agents,
        ):
            connections.return_value.exists.return_value = True
            agents.return_value.exists.return_value = True
            call_command("verify_agentic_audit", stdout=output)

        rendered = output.getvalue()
        self.assertIn("PASS\totlp-correlation", rendered)
        self.assertIn("PASS\ttrace-before-judge", rendered)
        self.assertNotIn("SKIP\ttrace-before-judge", rendered)
