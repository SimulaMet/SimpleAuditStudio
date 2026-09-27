from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Project, ProjectMembership, User
from audits.models import AuditRun
from model_registry.models import ModelConnection, RegisteredModel
from scenarios.models import Scenario, ScenarioRevision, ScenarioSet, ScenarioSetVersion


class AuditRunFreezeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pass12345")
        self.project = Project.objects.create(name="Research", slug="research")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client.force_authenticate(user=self.user)

        scenario = Scenario.objects.create(project=self.project, key="dose", title="Dose")
        revision = ScenarioRevision.objects.create(
            scenario=scenario,
            revision=1,
            description="Ask dose.",
            expected_behavior=["Safe"],
            test_prompt="What dose?",
            metadata={},
            content_hash="sha256:abc",
        )
        scenario_set = ScenarioSet.objects.create(project=self.project, name="Safety")
        self.version = ScenarioSetVersion.objects.create(scenario_set=scenario_set, version=1, scenario_count=1, content_hash="sha256:set")
        from scenarios.models import ScenarioSetVersionItem

        ScenarioSetVersionItem.objects.create(version=self.version, scenario=scenario, revision=revision, position=1)

        self.target_conn = ModelConnection.objects.create(
            project=self.project, name="Target conn", provider="simulachat",
            base_url="https://target.invalid/v1", secret_reference="TARGET_KEY",
        )
        self.auditor_conn = ModelConnection.objects.create(
            project=self.project, name="Auditor conn", provider="simulachat",
            base_url="https://auditor.invalid/v1", secret_reference="AUDITOR_KEY",
        )
        self.judge_conn = ModelConnection.objects.create(
            project=self.project, name="Judge conn", provider="simulachat",
            base_url="https://judge.invalid/v1", secret_reference="JUDGE_KEY",
        )
        self.target = RegisteredModel.objects.create(
            connection=self.target_conn, project=self.project,
            display_name="Target", model_id="target-model",
        )
        self.auditor = RegisteredModel.objects.create(
            connection=self.auditor_conn, project=self.project,
            display_name="Auditor", model_id="auditor-model",
        )
        self.judge = RegisteredModel.objects.create(
            connection=self.judge_conn, project=self.project,
            display_name="Judge", model_id="judge-model",
        )

    def test_create_audit_run_freezes_inputs_and_stamps_metadata_provenance(self):
        from unittest import mock

        # Provenance is authoritative: it comes from the installed package
        # metadata, not from the request body or settings.
        with mock.patch(
            "audits.services.resolve_engine_provenance",
            return_value=mock.Mock(version="0.1.0", commit="deadbeef", source="metadata"),
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/audit-runs/create/",
                {
                    "name": "Baseline audit",
                    "scenario_set_version_id": self.version.id,
                    "target_model_id": self.target.id,
                    "auditor_model_id": self.auditor.id,
                    "judge_model_id": self.judge.id,
                },
                format="json",
            )
        assert response.status_code == 201, response.content
        payload = response.json()
        run = AuditRun.objects.get(id=payload["id"])
        assert run.status == AuditRun.Status.QUEUED
        assert run.simpleaudit_version == "0.1.0"
        assert run.git_commit == "deadbeef"
        assert run.total_scenarios == 1
        assert run.target_config_snapshot["secret_reference"] == "TARGET_KEY"
        assert "api_key_direct" not in run.target_config_snapshot
        assert run.scenario_set_version_id == self.version.id

        detail = self.client.get(f"/api/projects/{self.project.id}/audit-runs/{run.id}/")
        assert detail.status_code == 200, detail.content
        detail_payload = detail.json()
        assert detail_payload["scenario_set_version_hash"] == "sha256:set"
        assert detail_payload["target_config_snapshot"]["model_id"] == "target-model"
        assert detail_payload["simpleaudit_version"] == "0.1.0"

    def test_create_audit_run_allows_missing_commit_for_registry_install(self):
        from unittest import mock

        # A registry install has no git commit; that must NOT block run creation.
        with mock.patch(
            "audits.services.resolve_engine_provenance",
            return_value=mock.Mock(version="0.1.13", commit=None, source="metadata"),
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/audit-runs/create/",
                {
                    "name": "Registry install run",
                    "scenario_set_version_id": self.version.id,
                    "target_model_id": self.target.id,
                    "auditor_model_id": self.auditor.id,
                    "judge_model_id": self.judge.id,
                },
                format="json",
            )
        assert response.status_code == 201, response.content
        run = AuditRun.objects.get(id=response.json()["id"])
        assert run.simpleaudit_version == "0.1.13"
        assert run.git_commit == ""

    def test_create_audit_run_refuses_when_engine_not_installed(self):
        from unittest import mock

        # No engine installed -> no version -> run creation must be refused.
        with mock.patch(
            "audits.services.resolve_engine_provenance",
            return_value=mock.Mock(version=None, commit=None, source="unavailable"),
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/audit-runs/create/",
                {
                    "name": "No engine",
                    "scenario_set_version_id": self.version.id,
                    "target_model_id": self.target.id,
                    "auditor_model_id": self.auditor.id,
                    "judge_model_id": self.judge.id,
                },
                format="json",
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "simpleaudit_provenance_required"

    def test_create_audit_run_rejects_cross_project_scenario_version(self):
        other_project = Project.objects.create(name="Other", slug="other")
        other_set = ScenarioSet.objects.create(project=other_project, name="Other set")
        other_version = ScenarioSetVersion.objects.create(scenario_set=other_set, version=1, scenario_count=0, content_hash="sha256:other")

        response = self.client.post(
            f"/api/projects/{self.project.id}/audit-runs/create/",
            {
                "name": "Cross project",
                "scenario_set_version_id": other_version.id,
                "target_model_id": self.target.id,
                "auditor_model_id": self.auditor.id,
                "judge_model_id": self.judge.id,
                "simpleaudit_version": "0.1.0",
                "git_commit": "deadbeef",
            },
            format="json",
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "audit_input_not_found"


class GenerationParametersPriorityTests(TestCase):
    """Explicit form fields must override same keys in gen_config JSON."""

    def test_max_turns_form_wins_over_json(self):
        from audits.services import _generation_parameters

        params = _generation_parameters(
            max_turns_override=6,
            gen_config_override={"max_turns": 4, "language": "English"},
        )
        assert params["max_turns"] == 6
        # language is a form-managed key; stripped from JSON when no form override given
        assert "language" not in params

    def test_language_form_wins_over_json(self):
        from audits.services import _generation_parameters

        params = _generation_parameters(
            language_override="Norwegian",
            gen_config_override={"language": "English", "max_retries": 3},
        )
        assert params["language"] == "Norwegian"
        assert params["max_retries"] == 3

    def test_n_reps_form_wins_over_json(self):
        from audits.services import _generation_parameters

        params = _generation_parameters(
            n_repetitions_override=5,
            gen_config_override={"n_repetitions": 2},
        )
        assert params["n_repetitions"] == 5

    def test_form_managed_keys_stripped_from_json(self):
        """max_turns/n_repetitions/language in JSON are stripped; only non-form keys pass through."""
        from audits.services import _generation_parameters

        params = _generation_parameters(
            gen_config_override={"max_turns": 4, "n_repetitions": 2, "language": "English", "system_prompt": "be nice"},
        )
        assert "max_turns" not in params
        assert "n_repetitions" not in params
        assert "language" not in params
        assert params["system_prompt"] == "be nice"

    def test_form_fields_set_when_provided(self):
        from audits.services import _generation_parameters

        params = _generation_parameters(
            max_turns_override=6,
            language_override="Norwegian",
            n_repetitions_override=3,
            gen_config_override={"max_turns": 4, "language": "English", "n_repetitions": 2, "max_retries": 5},
        )
        assert params["max_turns"] == 6
        assert params["language"] == "Norwegian"
        assert params["n_repetitions"] == 3
        assert params["max_retries"] == 5
