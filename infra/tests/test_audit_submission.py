"""Tests for durable audit submission (freeze -> enqueue).

Verifies that submitting a frozen AuditRun degrades gracefully when no live
Hatchet server is available: the run stays queued, the reason is recorded in
runtime_metadata, and the immutable frozen inputs are untouched.
"""
from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from audits.models import AuditRun
from audits.services import create_audit_run, submit_audit_run
from model_registry.models import ModelConnection, RegisteredModel
from scenarios.models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


class AuditSubmissionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pass12345")
        self.project = Project.objects.create(name="Research", slug="research")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)

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
        self.version = ScenarioSetVersion.objects.create(
            scenario_set=scenario_set, version=1, scenario_count=1, content_hash="sha256:set"
        )
        ScenarioSetVersionItem.objects.create(version=self.version, scenario=scenario, revision=revision, position=1)

        self.target_conn = ModelConnection.objects.create(
            project=self.project, name="T conn", provider="simulachat",
            base_url="https://target.invalid/v1", secret_reference="TARGET_KEY",
        )
        self.auditor_conn = ModelConnection.objects.create(
            project=self.project, name="A conn", provider="simulachat",
            base_url="https://auditor.invalid/v1", secret_reference="AUDITOR_KEY",
        )
        self.judge_conn = ModelConnection.objects.create(
            project=self.project, name="J conn", provider="simulachat",
            base_url="https://judge.invalid/v1", secret_reference="JUDGE_KEY",
        )
        self.target = RegisteredModel.objects.create(connection=self.target_conn, project=self.project, display_name="Target", model_id="target-model")
        self.auditor = RegisteredModel.objects.create(connection=self.auditor_conn, project=self.project, display_name="Auditor", model_id="auditor-model")
        self.judge = RegisteredModel.objects.create(connection=self.judge_conn, project=self.project, display_name="Judge", model_id="judge-model")

    def _make_run(self) -> AuditRun:
        from unittest import mock

        with mock.patch(
            "audits.services.resolve_engine_provenance",
            return_value=mock.Mock(version="0.1.0", commit="deadbeef", source="metadata"),
        ):
            return create_audit_run(
                project=self.project,
                user=self.user,
                name="Baseline",
                scenario_set_version=self.version,
                target_model=self.target,
                auditor_model=self.auditor,
                judge_model=self.judge,
            )

    def test_submit_without_live_server_keeps_run_queued_and_records_reason(self):
        run = self._make_run()
        snapshot_before = run.target_config_snapshot
        version_before = run.scenario_set_version_id

        # Simulate no live Hatchet server: get_client() raises so submission
        # degrades gracefully regardless of whether a server is actually reachable.
        from unittest import mock
        with mock.patch("infra.worker.get_client", side_effect=RuntimeError("no hatchet server")):
            result = submit_audit_run(run)
        self.assertIsNone(result)

        run.refresh_from_db()
        # The frozen experiment record is intact and still queued.
        self.assertEqual(run.status, AuditRun.Status.QUEUED)
        self.assertEqual(run.target_config_snapshot, snapshot_before)
        self.assertEqual(run.scenario_set_version_id, version_before)
        # The failure reason is recorded so an operator can resubmit.
        submission = run.runtime_metadata.get("submission", {})
        self.assertEqual(submission.get("status"), "pending")
        self.assertIn("reason", submission)
