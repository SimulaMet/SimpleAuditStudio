"""Operator extensions (issue #83): manifest, menu, slots, page context, embed, authz."""
import json
import re

from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, TestCase, override_settings

from infra.extensions import load_extensions, parse_manifest
from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory

MANIFEST = [
    {"slug": "feedback", "label": "Feedback", "icon": "👍", "section": "Tools", "embed": "/feedback/"},
    {"slug": "docs", "label": "Team docs", "href": "https://docs.example.org/", "roles": ["viewer"]},
    {
        "slug": "skin", "roles": ["user"], "css": "/static/ext/skin.css",
        "js": ["/static/ext/skin.js"], "pages": ["/scenarios/*"],
        "integrity": {"/static/ext/skin.js": "sha384-abc123"},
    },
]


def manifest(entries=MANIFEST):
    return override_settings(SIMPLEAUDIT_EXTENSIONS=json.dumps(entries))


class ManifestParsingTests(SimpleTestCase):
    def test_parses_and_sorts(self):
        exts = parse_manifest(json.dumps(MANIFEST))
        self.assertEqual([e.slug for e in exts], ["skin", "docs", "feedback"])   # by section, then order
        feedback = exts[2]
        skin = exts[0]
        self.assertEqual(feedback.url, "/x/feedback/")
        self.assertTrue(skin.applies_to("/scenarios/12/"))
        self.assertFalse(skin.applies_to("/connections/"))

    def test_wrapped_form(self):
        self.assertEqual(len(parse_manifest(json.dumps({"extensions": MANIFEST}))), 3)

    def test_rejects_bad_entries(self):
        bad = [
            [{"slug": "Bad Slug"}],
            [{"slug": "a", "roles": ["root"]}],
            [{"slug": "a", "href": "/x", "embed": "/y"}],
            [{"slug": "a", "embed": "https://elsewhere.example/"}],
            [{"slug": "a", "embed": "//elsewhere.example/"}],
            [{"slug": "a", "href": "javascript:alert(1)"}],
            [{"slug": "a", "js": "data:text/javascript,alert(1)"}],
            [{"slug": "a", "integrity": {"/x.js": "md5-zzz"}}],
            [{"slug": "a"}, {"slug": "a"}],
            {"not": "a list"},
        ]
        for entries in bad:
            with self.assertRaises(ImproperlyConfigured, msg=entries):
                parse_manifest(json.dumps(entries))
        with self.assertRaises(ImproperlyConfigured):
            parse_manifest("{not json")

    def test_documented_example_manifest_is_valid(self):
        from pathlib import Path

        from django.conf import settings

        text = (Path(settings.BASE_DIR) / "docs" / "examples" / "extensions.json").read_text()
        self.assertEqual([e.slug for e in parse_manifest(text)], ["hello", "handbook", "feedback"])
        for asset in ("hello.js", "hello.css"):
            self.assertTrue((Path(settings.BASE_DIR) / "static" / "extensions" / "example" / asset).exists())

    @override_settings(SIMPLEAUDIT_EXTENSIONS="[{\"slug\": \"BAD\"}]")
    def test_invalid_manifest_disables_instead_of_crashing(self):
        with self.assertLogs("simpleaudit.extensions", "ERROR"):
            self.assertEqual(load_extensions(), ())

    def test_manifest_from_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(MANIFEST[:1], fh)
        with override_settings(SIMPLEAUDIT_EXTENSIONS=fh.name):
            self.assertEqual([e.slug for e in load_extensions()], ["feedback"])
        with override_settings(SIMPLEAUDIT_EXTENSIONS="/nonexistent/extensions.json"), \
                self.assertLogs("simpleaudit.extensions", "ERROR"):
            self.assertEqual(load_extensions(), ())


class ExtensionPageTests(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        # Extension admin comes from is_staff; the viewer membership only gives an active workspace.
        self.admin = UserFactory(is_staff=True)
        MembershipFactory(user=self.admin, project=self.project, role="viewer")
        self.viewer = UserFactory()
        MembershipFactory(user=self.viewer, project=self.project, role="viewer")

    def _client(self, user):
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_project_id"] = self.project.id
        session.save()
        return client

    def _page(self, user, path="/"):
        resp = self._client(user).get(path)
        self.assertEqual(resp.status_code, 200, path)
        return resp.content.decode()

    @override_settings(SIMPLEAUDIT_EXTENSIONS="")
    def test_unset_adds_nothing(self):
        page = self._page(self.admin)
        self.assertNotIn("studio-extension", page)
        self.assertNotIn("studio-page-context", page)
        self.assertNotIn("studio:ready", page)
        self.assertEqual(self._client(self.admin).get("/x/feedback/").status_code, 404)
        self.assertEqual(self._client(self.admin).get("/ext/authz").status_code, 404)

    def test_menu_items_filtered_by_role_on_the_server(self):
        with manifest():
            admin_page = self._page(self.admin)
            viewer_page = self._page(self.viewer)
        self.assertIn('href="/x/feedback/"', admin_page)
        self.assertIn('href="https://docs.example.org/"', admin_page)
        self.assertIn(">Tools<", admin_page)
        self.assertNotIn("/x/feedback/", viewer_page)          # admin-only: absent, not hidden
        self.assertNotIn("Feedback", viewer_page)
        self.assertIn('href="https://docs.example.org/"', viewer_page)

    def test_assets_in_slots_with_integrity_and_defer(self):
        with manifest():
            page = self._page(self.viewer, "/scenarios/")
            other = self._page(self.viewer, "/connections/")
        head, body = page.split("</head>", 1)
        self.assertRegex(head, r'<link rel="stylesheet" href="/static/ext/skin.css"[^>]*data-studio-extension>')
        self.assertIn(
            '<script src="/static/ext/skin.js" defer integrity="sha384-abc123" crossorigin="anonymous"', body,
        )
        self.assertNotIn("skin.js", head)
        self.assertNotIn("skin.css", other)                       # pages: ["/scenarios/*"]
        self.assertNotIn("skin.js", other)

    def test_window_studio_context_precedes_scripts(self):
        with manifest():
            page = self._page(self.admin, "/scenarios/")
        match = re.search(r'<script id="studio-page-context" type="application/json">(.*?)</script>', page, re.DOTALL)
        self.assertIsNotNone(match)
        ctx = json.loads(match.group(1))
        self.assertEqual(ctx["user"]["username"], self.admin.username)
        self.assertEqual(ctx["role"], "admin")
        self.assertEqual(ctx["workspace"]["id"], self.project.id)
        self.assertTrue(ctx["csrfToken"])
        self.assertIn("version", ctx)
        self.assertEqual(ctx["extensions"], ["skin"])
        self.assertLess(page.index("studio-page-context"), page.index("/static/ext/skin.js"))
        self.assertIn("studio:ready", page)
        self.assertIn("window.Studio = ctx", page)

    def test_embed_page(self):
        with manifest():
            page = self._page(self.admin, "/x/feedback/")
            self.assertIn('<iframe src="/feedback/"', page)
            self.assertIn('aria-current="page" data-studio-extension="feedback"', page)
            self.assertEqual(self._client(self.viewer).get("/x/feedback/").status_code, 404)   # forbidden = 404
            self.assertEqual(self._client(self.admin).get("/x/unknown/").status_code, 404)
            self.assertEqual(self._client(self.admin).get("/x/docs/").status_code, 404)        # href, not embed
            anon = Client().get("/x/feedback/")
            self.assertEqual(anon.status_code, 302)
            self.assertIn("/login/", anon["Location"])

    def test_workspace_roles_count_up_to_auditor(self):
        ws_admin = UserFactory()
        MembershipFactory(user=ws_admin, project=self.project, role="admin")
        entries = MANIFEST + [{"slug": "review", "label": "Review", "embed": "/review/", "roles": ["auditor"]}]
        with manifest(entries):
            page = self._page(ws_admin, "/scenarios/")
            self.assertIn('href="/x/review/"', page)
            self.assertNotIn("/x/feedback/", page)
            ctx = json.loads(re.search(
                r'<script id="studio-page-context" type="application/json">(.*?)</script>', page, re.DOTALL,
            ).group(1))
            self.assertEqual(ctx["role"], "auditor")
            self.assertEqual(self._client(ws_admin).get("/ext/authz?role=auditor").status_code, 200)
            self.assertEqual(self._client(ws_admin).get("/ext/authz?role=admin").status_code, 403)
            self.assertEqual(self._client(ws_admin).get("/x/feedback/").status_code, 404)

    def test_authz(self):
        with manifest():
            ok = self._client(self.admin).get("/ext/authz?role=admin")
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok["X-Studio-Role"], "user")   # chat.config.identity(): no workspace admin row
            self.assertEqual(ok["X-Studio-Username"], self.admin.username)
            self.assertIn("X-Studio-Email", ok)
            self.assertEqual(self._client(self.viewer).get("/ext/authz").status_code, 403)   # default role admin
            viewer_ok = self._client(self.viewer).get("/ext/authz?role=viewer")
            self.assertEqual(viewer_ok.status_code, 200)
            self.assertEqual(viewer_ok["X-Studio-Role"], "user")
            anon = Client().get("/ext/authz", HTTP_X_FORWARDED_URI="/feedback/?x=1")
            self.assertEqual(anon.status_code, 401)
            self.assertEqual(anon["X-Studio-Login"], "/login/?next=%2Ffeedback%2F%3Fx%3D1")
            self.assertEqual(self._client(self.admin).get("/ext/authz?role=root").status_code, 400)
