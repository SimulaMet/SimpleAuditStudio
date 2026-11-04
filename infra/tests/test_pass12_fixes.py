"""Regression tests for audit pass 12: visualizer API, connection edits, KB external_id, untrusted spans."""
import json
import os
import tempfile
from unittest import mock

from django.test import TestCase, override_settings

from accounts.models import Project, ProjectMembership, User


def _login(client, user, project):
    client.force_login(user)
    session = client.session
    session["active_project_id"] = project.id
    session.save()


class VisualizerApiAuthTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "r.json"), "w") as f:
            json.dump([{"scenario": "x"}], f)

    def test_api_requires_login(self):
        with mock.patch("infra.visualizer.results_dir", return_value=self.tmp):
            for url in ["/api/visualizer/api/files/", "/api/visualizer/api/json/r.json",
                        "/api/visualizer/api/image/?uri=/etc/hosts"]:
                resp = self.client.get(url)
                self.assertIn(resp.status_code, (302, 401, 403), url)

    def test_image_outside_results_dir_refused(self):
        user = User.objects.create_user(username="vz", password="pass12345")
        self.client.force_login(user)
        with mock.patch("infra.visualizer.results_dir", return_value=self.tmp):
            for uri in ["/etc/hosts", "../../etc/hosts"]:
                resp = self.client.get("/api/visualizer/api/image/", {"uri": uri})
                self.assertEqual(resp.status_code, 403, uri)
            for uri in ["s3://bucket/x.png", "file:///etc/hosts"]:
                with mock.patch("simpleaudit.utils.image_data_uri") as loader:
                    resp = self.client.get("/api/visualizer/api/image/", {"uri": uri})
                self.assertEqual(resp.status_code, 415, uri)
                loader.assert_not_called()

    def test_list_shaped_result_file_does_not_500(self):
        user = User.objects.create_user(username="vz2", password="pass12345")
        self.client.force_login(user)
        self.client.raise_request_exception = False
        with mock.patch("infra.visualizer.results_dir", return_value=self.tmp), \
             mock.patch("infra.visualizer.is_valid_audit_data", return_value=True):
            resp = self.client.get("/api/visualizer/api/json/r.json")
        self.assertLess(resp.status_code, 500)


class AuditorConnectionEditTest(TestCase):
    def setUp(self):
        from infra.tests.factories import ModelConnectionFactory

        self.project = Project.objects.create(name="C", slug="c")
        self.auditor = User.objects.create_user(username="aud", password="pass12345")
        ProjectMembership.objects.create(project=self.project, user=self.auditor, role=ProjectMembership.Role.AUDITOR)
        self.conn = ModelConnectionFactory(project=self.project, base_url="https://api.openai.com/v1", api_key_direct="sk-admin")
        _login(self.client, self.auditor, self.project)

    def test_auditor_cannot_repoint_connection(self):
        self.client.post("/connections/", {
            "action": "edit_connection", "conn_id": self.conn.id, "conn_name": "x",
            "conn_base_url": "https://attacker.example/v1", "conn_provider": "openai", "key_mode": "stored",
        })
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.base_url, "https://api.openai.com/v1")

    def test_auditor_cannot_delete_connection(self):
        from model_registry.models import ModelConnection

        self.client.post(f"/connections/{self.conn.id}/delete/")
        self.assertTrue(ModelConnection.objects.filter(pk=self.conn.pk).exists())


class KnowledgeBaseExternalIdTest(TestCase):
    def test_external_id_cannot_be_changed_by_update(self):
        from model_registry.models import KnowledgeBase

        project = Project.objects.create(name="K", slug="k")
        user = User.objects.create_user(username="kb", password="pass12345")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.AUDITOR)
        kb = KnowledgeBase.objects.create(project=project, name="KB", external_id="mine")
        _login(self.client, user, project)
        resp = self.client.put(f"/api/knowledge-bases/{kb.id}/", {"name": "KB", "external_id": "victim"},
                               content_type="application/json")
        self.assertLess(resp.status_code, 500)
        kb.refresh_from_db()
        self.assertEqual(kb.external_id, "mine")


class UntrustedSpanTest(TestCase):
    def test_malformed_tool_calls_do_not_raise(self):
        from infra.ui import _llm_tool_decision

        # GenAI output messages: role/parts, a tool call is a {"type": "tool_call"} part.
        for msgs in ([{"parts": 5}], [{"parts": [{"type": "tool_call"}]}], [{"parts": ["a"]}], ["x"], {"a": 1}):
            self.assertIsNone(_llm_tool_decision({}, {"gen_ai.output.messages": json.dumps(msgs)}))
        ok = [{"role": "assistant", "parts": [{"type": "tool_call", "name": "lookup", "arguments": {}}]}]
        self.assertIsNotNone(_llm_tool_decision({}, {"gen_ai.output.messages": json.dumps(ok)}))


class PromptJudgeBadIdsTest(TestCase):
    def test_non_numeric_ids_do_not_500(self):
        user = User.objects.create_user(username="pj", password="pass12345")
        project = Project.objects.create(name="PJ", slug="pj")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
        _login(self.client, user, project)
        self.client.raise_request_exception = False
        for url in ["/prompts/new/?clone=abc", "/judges/new/?clone=abc"]:
            self.assertLess(self.client.get(url).status_code, 500, url)
        for url, data in [("/prompts/", {"action": "delete", "prompt_id": "abc"}),
                          ("/prompts/", {"action": "delete_version", "version_id": "abc"}),
                          ("/judges/", {"action": "delete", "judge_id": "abc"}),
                          ("/judges/", {"action": "delete_version", "version_id": "abc"})]:
            self.assertLess(self.client.post(url, data).status_code, 500, data)


@override_settings()
class AgentPromptDataAttributeTest(TestCase):
    def test_prompt_versions_not_escapejs_in_attributes(self):
        for path in ("templates/agents/agent_detail.html", "templates/partials/run_settings.html"):
            with open(path) as f:
                self.assertNotIn("content|escapejs", f.read(), path)


class ToolCallNamesTest(TestCase):
    def test_all_names_returned_for_multi_tool_step(self):
        from infra.ui import _llm_tool_call_names

        msgs = [{"tool_calls": [{"function": {"name": "a"}}, {"function": {"name": "b"}}, {"function": {"name": "c"}}]}]
        self.assertEqual(_llm_tool_call_names({"gen_ai.output.messages": json.dumps(msgs)}), ["a", "b", "c"])
