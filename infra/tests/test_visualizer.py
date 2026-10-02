"""Result visualizer endpoints: file tree, JSON/image APIs, and HTML export.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_visualizer
"""
import json
import os

from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    ScenarioResultFactory,
    UserFactory,
)
from infra.visualizer import (
    build_standalone_html,
    is_valid_audit_data,
    set_results_dir,
)


def _valid_single():
    return {"results": [{"scenario_name": "s1", "severity": "pass", "summary": "ok"}]}


def _valid_experiment():
    return {
        "runs": {
            "model-a": [
                {"results": [{"scenario_name": "s1", "severity": "high", "summary": "bad"}]}
            ]
        }
    }


class _VisualizerBase(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        self.project = ProjectFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")


class VisualizerShapeTests(TestCase):
    def test_is_valid_audit_data_shapes(self):
        self.assertTrue(is_valid_audit_data(_valid_single()))
        self.assertTrue(is_valid_audit_data(_valid_experiment()))
        self.assertTrue(is_valid_audit_data([{"scenario_name": "s", "severity": "pass"}]))
        self.assertFalse(is_valid_audit_data({"foo": "bar"}))
        self.assertFalse(is_valid_audit_data([]))
        self.assertFalse(is_valid_audit_data(None))

    def test_build_standalone_html_inlines_data(self):
        html = build_standalone_html(_valid_single(), "my run")
        self.assertIn("window.__inlinedData", html)
        self.assertIn("window.__standaloneMode", html)
        self.assertIn("my run", html)
        # The payload must not break out of the script tag.
        self.assertNotIn("<script>window.__inlinedData = {\"results\": [{\"scenario_name\": \"s1\", \"severity\": \"pass\", \"summary\": \"ok\"}]}</script>", html.replace("<", "<"))
        self.assertIn("scenario_name", html)

    def test_build_standalone_html_rejects_invalid(self):
        with self.assertRaises(ValueError):
            build_standalone_html({"foo": "bar"}, "x")


class VisualizerPageTests(_VisualizerBase):
    def test_visualizer_page_renders_and_injects_api_base(self):
        resp = self.client.get("/visualizer", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "window.__VISUALIZER_API_BASE = '/api/visualizer'")
        self.assertContains(resp, "SimpleAudit Result Visualizer")

    def test_scenario_viewer_page_renders(self):
        resp = self.client.get("/visualizer/upload", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "file-drop-zone")

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get("/visualizer", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sign in")


class VisualizerFilesTests(_VisualizerBase):
    def setUp(self):
        super().setUp()
        self.results = self._make_results_dir()
        set_results_dir(self.results)
        self.addCleanup(set_results_dir, None)

    def _make_results_dir(self):
        import tempfile

        d = tempfile.mkdtemp(prefix="visz_")
        self.addCleanup(_rmtree, d)
        os.makedirs(os.path.join(d, "sub"), exist_ok=True)
        with open(os.path.join(d, "single.json"), "w") as f:
            json.dump(_valid_single(), f)
        with open(os.path.join(d, "experiment.json"), "w") as f:
            json.dump(_valid_experiment(), f)
        with open(os.path.join(d, "sub", "nested.json"), "w") as f:
            json.dump(_valid_single(), f)
        with open(os.path.join(d, "not_audit.json"), "w") as f:
            json.dump({"foo": "bar"}, f)
        with open(os.path.join(d, "readme.txt"), "w") as f:
            f.write("not json")
        return d

    def test_files_tree_lists_valid_files_and_folders(self):
        resp = self.client.get("/api/visualizer/api/files", follow=True)
        self.assertEqual(resp.status_code, 200)
        tree = resp.json()["tree"]
        names = {item["name"]: item for item in tree}
        self.assertIn("single.json", names)
        self.assertEqual(names["single.json"]["type"], "file")
        self.assertIn("experiment.json", names)
        self.assertEqual(names["experiment.json"]["type"], "experiment")
        self.assertEqual(names["experiment.json"]["models"], ["model-a"])
        self.assertIn("sub", names)
        self.assertEqual(names["sub"]["type"], "folder")
        # Non-audit JSON and non-JSON files are excluded.
        self.assertNotIn("not_audit.json", names)
        self.assertNotIn("readme.txt", names)
        # The nested file shows up under the folder.
        self.assertEqual([c["name"] for c in names["sub"]["children"]], ["nested.json"])

    def test_files_without_results_dir(self):
        set_results_dir(None)
        resp = self.client.get("/api/visualizer/api/files", follow=True)
        self.assertEqual(resp.status_code, 500)


class VisualizerJsonTests(_VisualizerBase):
    def setUp(self):
        super().setUp()
        self.results = self._make_results_dir()
        set_results_dir(self.results)
        self.addCleanup(set_results_dir, None)

    def _make_results_dir(self):
        import tempfile

        d = tempfile.mkdtemp(prefix="visz_json_")
        self.addCleanup(_rmtree, d)
        os.makedirs(os.path.join(d, "sub"), exist_ok=True)
        with open(os.path.join(d, "single.json"), "w") as f:
            json.dump(_valid_single(), f)
        with open(os.path.join(d, "sub", "nested.json"), "w") as f:
            json.dump(_valid_single(), f)
        with open(os.path.join(d, "not_audit.json"), "w") as f:
            json.dump({"foo": "bar"}, f)
        return d

    def test_json_returns_valid_audit_file(self):
        resp = self.client.get("/api/visualizer/api/json/single.json", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), _valid_single())

    def test_json_returns_nested_file(self):
        resp = self.client.get("/api/visualizer/api/json/sub/nested.json", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), _valid_single())

    def test_json_rejects_path_traversal(self):
        resp = self.client.get("/api/visualizer/api/json/../../etc/passwd", follow=True)
        self.assertIn(resp.status_code, (403, 404))

    def test_json_rejects_non_audit_file(self):
        resp = self.client.get("/api/visualizer/api/json/not_audit.json", follow=True)
        self.assertEqual(resp.status_code, 403)

    def test_json_404_when_missing(self):
        resp = self.client.get("/api/visualizer/api/json/missing.json", follow=True)
        self.assertEqual(resp.status_code, 404)


class VisualizerImageTests(_VisualizerBase):
    def test_image_missing_uri(self):
        resp = self.client.get("/api/visualizer/api/image", follow=True)
        self.assertEqual(resp.status_code, 400)

    def test_image_non_image_uri(self):
        resp = self.client.get("/api/visualizer/api/image/?uri=not-an-image://x", follow=True)
        self.assertEqual(resp.status_code, 415)


class RunExportHtmlTests(_VisualizerBase):
    def test_export_html_inlines_results(self):
        self.run = AuditRunFactory(project=self.project, status="completed")
        ScenarioResultFactory(run_id=self.run.id)
        resp = self.client.get(f"/runs/{self.run.id}/export-html/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("window.__inlinedData", resp.content.decode())
        self.assertIn("scenario_name", resp.content.decode())

    def test_export_html_404_for_other_project(self):
        other = AuditRunFactory(status="completed")
        resp = self.client.get(f"/runs/{other.id}/export-html/")
        self.assertEqual(resp.status_code, 404)


def _rmtree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)
