import json

from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from audits.models import AuditRun
from model_registry.models import ModelEndpoint
from scenarios.models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


class AuditCloneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="clone-user", password="test-password")
        self.project = Project.objects.create(name="Clone project", slug="clone-project")
        ProjectMembership.objects.create(
            project=self.project,
            user=self.user,
            role=ProjectMembership.Role.AUDITOR,
        )
        scenario = Scenario.objects.create(project=self.project, key="safety", title="Safety")
        revision = ScenarioRevision.objects.create(
            scenario=scenario,
            revision=1,
            description="Safety scenario",
            expected_behavior=["Safe"],
            test_prompt="Is this safe?",
            metadata={},
            content_hash="sha256:scenario",
        )
        scenario_set = ScenarioSet.objects.create(project=self.project, name="Safety set")
        self.version = ScenarioSetVersion.objects.create(
            scenario_set=scenario_set,
            version=7,
            scenario_count=1,
            content_hash="sha256:version-seven",
        )
        ScenarioSetVersionItem.objects.create(
            version=self.version,
            scenario=scenario,
            revision=revision,
            position=1,
        )
        self.target = self._endpoint("Target")
        self.auditor = self._endpoint("Auditor")
        self.judge = self._endpoint("Judge")
        self.run = AuditRun.objects.create(
            project=self.project,
            name="Original audit",
            scenario_set_version=self.version,
            target_endpoint=self.target,
            auditor_endpoint=self.auditor,
            judge_endpoint=self.judge,
            target_config_snapshot={"model_id": "target"},
            auditor_config_snapshot={"model_id": "auditor"},
            judge_config_snapshot={"model_id": "judge"},
            generation_parameters_snapshot={
                "max_turns": 9,
                "language": "Norwegian",
                "n_repetitions": 3,
                "target_params": {"temperature": 0.4},
            },
            simpleaudit_version="0.1.0",
            git_commit="abc123",
        )
        self.client.force_login(self.user)

    def _endpoint(self, name):
        return ModelEndpoint.objects.create(
            project=self.project,
            display_name=name,
            provider="test",
            base_url=f"https://{name.lower()}.invalid/v1",
            model_id=name.lower(),
        )

    def test_clone_prefills_exact_version_and_frozen_parameters(self):
        response = self.client.get(f"/audits/new/?clone_from={self.run.id}")

        self.assertEqual(response.status_code, 200)
        clone = response.context["clone"]
        self.assertEqual(clone["scenario_set_version_id"], self.version.id)
        self.assertEqual(clone["max_turns"], 9)
        self.assertEqual(clone["language"], "Norwegian")
        self.assertEqual(clone["n_repetitions"], 3)
        self.assertEqual(json.loads(clone["generation_json"])["target_params"]["temperature"], 0.4)
