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
        self.assertIn("https://huggingface.co/Qwen/Qwen3-30B-A3B", page)

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
        self.assertIn('href="https://huggingface.co/Qwen/Qwen3-30B-A3B"', page)
        self.assertNotIn("remapped-later", page)
