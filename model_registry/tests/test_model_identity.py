"""Model identity: validation, card links, editing, and freezing into run snapshots."""
from django.test import SimpleTestCase, TestCase

from accounts.models import Project, ProjectMembership, User
from audits.services import _endpoint_snapshot
from model_registry.identity import (
    IdentityError,
    card_url,
    identity_view,
    normalize_identity,
)
from model_registry.models import ModelConnection, RegisteredModel


class NormalizeIdentityTest(SimpleTestCase):
    def test_empty_kind_clears(self):
        self.assertEqual(normalize_identity("", "Qwen/Qwen3-30B-A3B"), {})

    def test_hf_id_and_url_paste(self):
        self.assertEqual(normalize_identity("hf", " Qwen/Qwen3-30B-A3B "), {"kind": "hf", "id": "Qwen/Qwen3-30B-A3B"})
        self.assertEqual(
            normalize_identity("hf", "https://huggingface.co/Qwen/Qwen3-30B-A3B/"),
            {"kind": "hf", "id": "Qwen/Qwen3-30B-A3B"},
        )

    def test_rejects_bad_input(self):
        for args in [("hf", "qwen3"), ("hf", ""), ("bogus", "x"), ("vendor", "", ""),
                     ("vendor", "gpt-4.1", "javascript:alert(1)")]:
            with self.assertRaises(IdentityError, msg=args):
                normalize_identity(*args)

    def test_vendor_and_system(self):
        self.assertEqual(normalize_identity("vendor", "gpt-4.1"), {"kind": "vendor", "id": "gpt-4.1"})
        self.assertEqual(
            normalize_identity("system", "", "https://example.org/card"),
            {"kind": "system", "url": "https://example.org/card"},
        )

    def test_card_url(self):
        self.assertEqual(card_url({"kind": "hf", "id": "Qwen/Qwen3-30B-A3B"}), "https://huggingface.co/Qwen/Qwen3-30B-A3B")
        self.assertEqual(card_url({"kind": "hf", "id": "x", "url": "https://e.org/c"}), "https://e.org/c")
        self.assertEqual(card_url({"kind": "vendor", "id": "gpt-4.1"}), "")
        self.assertEqual(card_url({"kind": "system", "url": "javascript:alert(1)"}), "")
        self.assertIsNone(identity_view({}))
        self.assertIsNone(identity_view(None))


class ModelIdentityEditTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="mi", password="pass12345")
        self.project = Project.objects.create(name="P", slug="p")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        self.conn = ModelConnection.objects.create(project=self.project, name="Sigma2", base_url="https://s.example/v1")
        self.rm = RegisteredModel.objects.create(
            connection=self.conn, project=self.project, model_id="qwen3-30b", display_name="Qwen3",
        )

    def _edit(self, **identity):
        return self.client.post("/connections/", {
            "action": "edit_model", "rm_id": self.rm.id, "model_id_new": "qwen3-30b",
            "model_display_name": "Qwen3", **identity,
        })

    def test_edit_sets_and_clears_identity(self):
        self._edit(identity_kind="hf", identity_id="Qwen/Qwen3-30B-A3B")
        self.rm.refresh_from_db()
        self.assertEqual(self.rm.model_identity, {"kind": "hf", "id": "Qwen/Qwen3-30B-A3B"})
        page = self.client.get("/connections/").content.decode()
        self.assertIn('href="https://huggingface.co/Qwen/Qwen3-30B-A3B"', page)

        self._edit(identity_kind="")
        self.rm.refresh_from_db()
        self.assertEqual(self.rm.model_identity, {})

    def test_invalid_identity_keeps_model_unchanged(self):
        self.rm.model_identity = {"kind": "vendor", "id": "gpt-4.1"}
        self.rm.save()
        resp = self._edit(identity_kind="hf", identity_id="not-an-hf-id")
        self.assertLess(resp.status_code, 500)
        self.rm.refresh_from_db()
        self.assertEqual(self.rm.model_identity, {"kind": "vendor", "id": "gpt-4.1"})

    def test_snapshot_freezes_identity(self):
        self.rm.model_identity = {"kind": "hf", "id": "Qwen/Qwen3-30B-A3B"}
        self.rm.save()
        snap = _endpoint_snapshot(self.rm)
        self.assertEqual(snap["model_identity"], {"kind": "hf", "id": "Qwen/Qwen3-30B-A3B"})
        # Later remapping does not alter the already-built snapshot.
        self.rm.model_identity = {}
        self.rm.save()
        self.assertEqual(snap["model_identity"]["id"], "Qwen/Qwen3-30B-A3B")


class RunPageIdentityTest(TestCase):
    def test_run_manifest_shows_frozen_identity_chip(self):
        from django.test import Client

        from infra.tests.factories import (
            AuditRunFactory,
            MembershipFactory,
            ModelConnectionFactory,
            ProjectFactory,
            RegisteredModelFactory,
            UserFactory,
        )

        project = ProjectFactory()
        user = UserFactory()
        MembershipFactory(user=user, project=project, role="admin")
        model = RegisteredModelFactory(
            connection=ModelConnectionFactory(project=project),
            model_identity={"kind": "hf", "id": "Qwen/Qwen3-30B-A3B"},
        )
        run = AuditRunFactory(project=project, target_model=model, target_config_snapshot=_endpoint_snapshot(model))
        model.model_identity = {"kind": "vendor", "id": "remapped-later"}
        model.save()
        client = Client()
        client.force_login(user)
        page = client.get(f"/runs/{run.id}/").content.decode()
        self.assertIn("https://huggingface.co/Qwen/Qwen3-30B-A3B", page)   # in the model modal payload
        self.assertNotIn("remapped-later", page)


class LeaderboardLinkTest(SimpleTestCase):
    TEMPLATE = 'https://euroeval.com/leaderboards/norwegian?highlight="{model}"&category=generative'

    def test_benchmark_id_overrides_id(self):
        from model_registry.identity import leaderboard_url

        self.assertEqual(
            leaderboard_url(self.TEMPLATE, {"kind": "hf", "id": "Qwen/Qwen3.8-27B"}),
            "https://euroeval.com/leaderboards/norwegian?highlight=%22Qwen/Qwen3.8-27B%22&category=generative",
        )
        self.assertEqual(
            leaderboard_url(self.TEMPLATE, {"kind": "vendor", "id": "gpt-4.1", "benchmark_id": "gpt-4.1-2025-04-14"}),
            "https://euroeval.com/leaderboards/norwegian?highlight=%22gpt-4.1-2025-04-14%22&category=generative",
        )
        self.assertEqual(leaderboard_url(self.TEMPLATE, {"kind": "system", "url": "https://e.org"}), "")
        self.assertEqual(leaderboard_url("", {"kind": "hf", "id": "a/b"}), "")
        # Characters that would break out of the query string are encoded.
        self.assertIn("highlight=%22a%26b%23c%22", leaderboard_url(self.TEMPLATE, {"kind": "vendor", "id": "a&b#c"}))

    def test_normalize_keeps_benchmark_id(self):
        self.assertEqual(
            normalize_identity("vendor", "gpt-4.1", "", "gpt-4.1-2025-04-14"),
            {"kind": "vendor", "id": "gpt-4.1", "benchmark_id": "gpt-4.1-2025-04-14"},
        )

    def test_template_validation(self):
        from model_registry.identity import validate_leaderboard_template

        self.assertEqual(validate_leaderboard_template("  "), "")
        self.assertEqual(validate_leaderboard_template(self.TEMPLATE), self.TEMPLATE)
        for bad in ["https://euroeval.com/leaderboards/norwegian", "javascript:alert('{model}')",
                    "https://e.org/?q={model} x", 5]:
            with self.assertRaises(IdentityError, msg=bad):
                validate_leaderboard_template(bad)


class LeaderboardSettingTest(TestCase):
    TEMPLATE = LeaderboardLinkTest.TEMPLATE

    def setUp(self):
        self.admin = User.objects.create_superuser(username="root", password="pass12345", email="r@example.org")
        self.project = Project.objects.create(name="P", slug="p")
        ProjectMembership.objects.create(project=self.project, user=self.admin, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.admin)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        conn = ModelConnection.objects.create(project=self.project, name="OpenAI", base_url="https://o.example/v1")
        RegisteredModel.objects.create(
            connection=conn, project=self.project, model_id="gpt-4.1", display_name="GPT-4.1",
            model_identity={"kind": "vendor", "id": "gpt-4.1", "benchmark_id": "gpt-4.1-2025-04-14"},
        )

    def _patch(self, value):
        import json

        return self.client.patch("/api/admin/settings/", json.dumps({"leaderboard_url_template": value}),
                                 content_type="application/json")

    def _payload(self):
        from accounts.services import get_site_setting
        from infra.templatetags.identity_tags import model_payload

        return model_payload(RegisteredModel.objects.get(model_id="gpt-4.1"),
                             get_site_setting("leaderboard_url_template") or "")

    def test_default_template(self):
        self.assertEqual(self.client.get("/api/admin/settings/").json()["leaderboard_url_template"], self.TEMPLATE)
        page = self.client.get("/connections/").content.decode()
        self.assertIn("highlight=%22gpt-4.1-2025-04-14%22&amp;category=generative", page)   # chip link
        self.assertNotIn('id="model-modal"', page)   # the modal is run-page only
        self.assertIn("highlight=%22gpt-4.1-2025-04-14%22&category=generative", self._payload()["leaderboardUrl"])

    def test_admin_changes_and_clears_template(self):
        self.assertEqual(self._patch("https://example.org/board?q={model}").status_code, 200)
        self.assertEqual(self._payload()["leaderboardUrl"], "https://example.org/board?q=gpt-4.1-2025-04-14")
        self.assertEqual(self._patch("").status_code, 200)
        self.assertEqual(self._payload()["leaderboardUrl"], "")

    def test_rejects_invalid_template_and_non_superuser(self):
        self.assertEqual(self._patch("https://example.org/no-placeholder").status_code, 400)
        self.assertEqual(self.client.get("/api/admin/settings/").json()["leaderboard_url_template"], self.TEMPLATE)
        other = User.objects.create_user(username="u2", password="pass12345")
        self.client.force_login(other)
        self.assertEqual(self._patch("https://example.org/?q={model}").status_code, 403)

    def test_admin_page_shows_setting(self):
        page = self.client.get("/admin-settings/").content.decode()
        self.assertIn('id="admin-leaderboard-url"', page)


class ModelModalTest(TestCase):
    def setUp(self):
        from infra.tests.factories import (
            AuditRunFactory,
            MembershipFactory,
            ModelConnectionFactory,
            ProjectFactory,
            RegisteredModelFactory,
            UserFactory,
        )

        self.project = ProjectFactory()
        self.user = UserFactory()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.model = RegisteredModelFactory(
            connection=ModelConnectionFactory(project=self.project),
            model_identity={"kind": "hf", "id": "Qwen/Qwen3.8-27B"},
        )
        self.make_run = lambda snap: AuditRunFactory(
            project=self.project, target_model=self.model, target_config_snapshot=snap,
        )

    def _modal_payloads(self, run):
        import html
        import json
        import re

        from django.test import Client

        client = Client()
        client.force_login(self.user)
        page = client.get(f"/runs/{run.id}/").content.decode()
        self.assertIn('id="model-modal"', page)
        return [json.loads(html.unescape(m)) for m in re.findall(r'data-model-modal="([^"]*)"', page)]

    def test_run_page_trigger_carries_card_benchmark_and_connection(self):
        target = self._modal_payloads(self.make_run(_endpoint_snapshot(self.model)))[0]
        self.assertEqual(target["role"], "Target")
        self.assertEqual(target["cardUrl"], "https://huggingface.co/Qwen/Qwen3.8-27B")
        self.assertIn("highlight=%22Qwen/Qwen3.8-27B%22", target["leaderboardUrl"])
        self.assertTrue(target["leaderboardEmbedUrl"].endswith("&embed=1"))
        self.assertEqual(target["connectionUrl"], f"/connections/#conn-{self.model.connection_id}")

    def test_pre_identity_snapshot_borrows_live_identity(self):
        snap = _endpoint_snapshot(self.model)
        del snap["model_identity"]   # a run frozen before identities existed
        target = self._modal_payloads(self.make_run(snap))[0]
        self.assertEqual(target["identityId"], "Qwen/Qwen3.8-27B")

    def test_recorded_empty_identity_is_kept(self):
        snap = {**_endpoint_snapshot(self.model), "model_identity": {}}
        target = self._modal_payloads(self.make_run(snap))[0]
        self.assertEqual(target["kind"], "")
        self.assertEqual(target["leaderboardUrl"], "")

    def test_embed_url_only_for_euroeval(self):
        from infra.templatetags.identity_tags import embed_url

        self.assertEqual(embed_url("https://euroeval.com/x?a=1"), "https://euroeval.com/x?a=1&embed=1")
        self.assertEqual(embed_url("https://example.org/x"), "https://example.org/x")
        self.assertEqual(embed_url(""), "")
