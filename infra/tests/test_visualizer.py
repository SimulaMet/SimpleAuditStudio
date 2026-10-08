"""Result visualizer endpoints: file tree, JSON/image APIs, and HTML export.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_visualizer
"""
import json
import os
import tempfile

from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ProjectFactory,
    ScenarioResultFactory,
    UserFactory,
)
from infra.visualizer import (
    _metadata_index,
    _scan_results_dir,
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
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"tree": [], "configured": False})


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


def _make_valid_json(path: str) -> None:
    """Write a minimal valid audit-results JSON file."""
    with open(path, "w") as f:
        json.dump({"results": [{"scenario_name": "s", "severity": "pass", "summary": "ok"}]}, f)


class BoundedScanTests(TestCase):
    """Tests for the bounded, incremental file-tree scan."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="visz_bounded_")
        self.addCleanup(_rmtree, self.tmp)
        # Clear the metadata index between tests.
        _metadata_index.clear()

    def _scan(self, **kwargs):
        defaults = {
            "max_depth": 8,
            "max_files": 5000,
            "time_budget": 5.0,
        }
        defaults.update(kwargs)
        return _scan_results_dir(self.tmp, **defaults)

    def test_small_tree_not_truncated(self):
        _make_valid_json(os.path.join(self.tmp, "a.json"))
        tree, meta = self._scan()
        self.assertFalse(meta["truncated"])
        self.assertIsNone(meta["reason"])
        self.assertEqual(meta["inspected"], 1)
        self.assertGreaterEqual(meta["elapsed_seconds"], 0)
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]["name"], "a.json")

    def test_file_limit_truncates(self):
        # Create 10 valid JSON files, limit to 3.
        for i in range(10):
            _make_valid_json(os.path.join(self.tmp, f"f{i:02d}.json"))
        tree, meta = self._scan(max_files=3)
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["reason"], "file_limit")
        self.assertEqual(meta["inspected"], 3)
        self.assertEqual(len(tree), 3)

    def test_depth_limit_truncates(self):
        # Create a file at depth 3, limit depth to 2.
        deep = os.path.join(self.tmp, "a", "b", "c")
        os.makedirs(deep, exist_ok=True)
        _make_valid_json(os.path.join(deep, "deep.json"))
        tree, meta = self._scan(max_depth=2)
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["reason"], "depth_limit")
        # The deep file should not be in the tree.
        self.assertEqual(tree, [])

    def test_time_budget_truncates(self):
        # Create many files and use a tight time budget (5ms).
        # The scan should inspect some files but not all 500.
        for i in range(500):
            _make_valid_json(os.path.join(self.tmp, f"f{i:03d}.json"))
        _, meta = self._scan(time_budget=0.005)  # 5ms
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["reason"], "time_budget")
        self.assertGreater(meta["inspected"], 0)
        self.assertLess(meta["inspected"], 500)

    def test_symlinks_are_skipped(self):
        _make_valid_json(os.path.join(self.tmp, "real.json"))
        os.symlink(os.path.join(self.tmp, "real.json"), os.path.join(self.tmp, "link.json"))
        tree, _ = self._scan()
        names = [item["name"] for item in tree]
        self.assertIn("real.json", names)
        self.assertNotIn("link.json", names)

    def test_symlinked_directory_not_followed(self):
        # Create a real dir with a file, and a symlink to it.
        real_dir = os.path.join(self.tmp, "realdir")
        os.makedirs(real_dir)
        _make_valid_json(os.path.join(real_dir, "inner.json"))
        os.symlink(real_dir, os.path.join(self.tmp, "linkdir"))
        tree, _ = self._scan()
        # The real dir's file should appear; the symlinked dir should not.
        folder_names = [item["name"] for item in tree if item["type"] == "folder"]
        self.assertIn("realdir", folder_names)
        self.assertNotIn("linkdir", folder_names)

    def test_oversized_file_skipped(self):
        # Create a file larger than the 100MB limit (use a sparse file).
        big_path = os.path.join(self.tmp, "big.json")
        with open(big_path, "w") as f:
            f.seek(101 * 1024 * 1024 - 1)
            f.write("\0")
        _make_valid_json(os.path.join(self.tmp, "small.json"))
        tree, _ = self._scan()
        names = [item["name"] for item in tree]
        self.assertIn("small.json", names)
        self.assertNotIn("big.json", names)

    def test_valid_results_beyond_cap_still_truncated(self):
        # 10 valid files, cap at 5 → truncated, but the 5 shown are valid.
        for i in range(10):
            _make_valid_json(os.path.join(self.tmp, f"v{i}.json"))
        tree, meta = self._scan(max_files=5)
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["reason"], "file_limit")
        self.assertEqual(len(tree), 5)
        for item in tree:
            self.assertEqual(item["type"], "file")

    def test_metadata_index_reuse(self):
        # Scan once, then scan again — the second scan should be faster
        # (metadata index hit) and produce the same tree.
        _make_valid_json(os.path.join(self.tmp, "a.json"))
        tree1, meta1 = self._scan()
        self.assertEqual(meta1["inspected"], 1)
        # Clear the in-memory tree cache (not the metadata index).
        from infra.visualizer import _file_tree_cache

        _file_tree_cache["data"] = None
        _file_tree_cache["key"] = None
        tree2, meta2 = self._scan()
        self.assertEqual(tree1, tree2)
        self.assertEqual(meta2["inspected"], 1)
        # The metadata index should have an entry for the file.
        self.assertGreater(len(_metadata_index), 0)

    def test_pruned_dirs_skipped(self):
        # Files inside pruned dirs should not appear.
        for d in ["node_modules", ".git", "__pycache__"]:
            p = os.path.join(self.tmp, d)
            os.makedirs(p, exist_ok=True)
            _make_valid_json(os.path.join(p, "hidden.json"))
        _make_valid_json(os.path.join(self.tmp, "visible.json"))
        tree, _ = self._scan()
        names = [item["name"] for item in tree]
        self.assertIn("visible.json", names)
        self.assertNotIn("node_modules", names)
        self.assertNotIn(".git", names)
        self.assertNotIn("__pycache__", names)

    def test_experiment_file_in_tree(self):
        exp = {
            "runs": {
                "model-a": [
                    {"results": [{"scenario_name": "s", "severity": "high", "summary": "x"}]}
                ]
            }
        }
        with open(os.path.join(self.tmp, "exp.json"), "w") as f:
            json.dump(exp, f)
        tree, _ = self._scan()
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]["type"], "experiment")
        self.assertEqual(tree[0]["models"], ["model-a"])

    def test_non_audit_json_excluded(self):
        with open(os.path.join(self.tmp, "notaudit.json"), "w") as f:
            json.dump({"foo": "bar"}, f)
        tree, meta = self._scan()
        self.assertEqual(tree, [])
        self.assertEqual(meta["inspected"], 1)


class VisualizerFilesMetaTests(_VisualizerBase):
    """Tests for the /api/files response metadata."""

    def setUp(self):
        super().setUp()
        self.results = tempfile.mkdtemp(prefix="visz_meta_")
        self.addCleanup(_rmtree, self.results)
        _make_valid_json(os.path.join(self.results, "a.json"))
        set_results_dir(self.results)
        self.addCleanup(set_results_dir, None)

    def test_response_includes_metadata(self):
        resp = self.client.get("/api/visualizer/api/files/", follow=True)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("tree", data)
        self.assertIn("truncated", data)
        self.assertIn("reason", data)
        self.assertIn("inspected", data)
        self.assertIn("elapsed_seconds", data)
        self.assertIn("configured", data)
        self.assertTrue(data["configured"])
        self.assertFalse(data["truncated"])
        self.assertIsNone(data["reason"])
        self.assertEqual(data["inspected"], 1)
        self.assertGreaterEqual(data["elapsed_seconds"], 0)

    def test_response_truncated_when_limit_hit(self):
        # Add more files than the default cap (5000) — use a low cap via env.
        import os as _os

        old = _os.environ.get("VISUALIZER_MAX_INSPECTED_FILES")
        _os.environ["VISUALIZER_MAX_INSPECTED_FILES"] = "2"
        self.addCleanup(lambda: _os.environ.pop("VISUALIZER_MAX_INSPECTED_FILES", None) if old is None else _os.environ.__setitem__("VISUALIZER_MAX_INSPECTED_FILES", old))
        # Clear the tree cache so the new limit takes effect.
        from infra.visualizer import _file_tree_cache

        _file_tree_cache["data"] = None
        _file_tree_cache["key"] = None
        # Add a third file.
        _make_valid_json(os.path.join(self.results, "b.json"))
        _make_valid_json(os.path.join(self.results, "c.json"))
        resp = self.client.get("/api/visualizer/api/files/", follow=True)
        data = resp.json()
        self.assertTrue(data["truncated"])
        self.assertEqual(data["reason"], "file_limit")
        self.assertEqual(data["inspected"], 2)
