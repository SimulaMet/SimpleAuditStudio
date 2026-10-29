"""System prompt versioning: create, save (no-op if unchanged), delete, pages.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_prompts
"""
from django.test import Client, TestCase, tag

from infra.tests.factories import (
    AgentFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    ScenarioSetFactory,
    ScenarioSetVersionFactory,
    UserFactory,
)
from prompts.models import SystemPrompt
from prompts.services import (
    create_prompt,
    delete_prompt,
    delete_version,
    save_version,
    unique_name,
)


@tag("slow")
class _Base(TestCase):
    def setUp(self):
        self.project = ProjectFactory()
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        MembershipFactory(user=self.user, project=self.project, role="admin")
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")


class PromptServiceTests(_Base):
    def test_create_prompt_makes_v1(self):
        p = create_prompt(project=self.project, name="Guardrails", content="Be safe.", user=self.user)
        self.assertEqual(p.name, "Guardrails")
        v1 = p.latest
        self.assertEqual((v1.version, v1.content, v1.note), (1, "Be safe.", "Created"))

    def test_duplicate_name_rejected(self):
        create_prompt(project=self.project, name="A", content="x")
        with self.assertRaises(ValueError, msg="already exists"):
            create_prompt(project=self.project, name="A", content="y")

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError, msg="required"):
            create_prompt(project=self.project, name="  ", content="x")

    def test_new_version_only_when_content_changes(self):
        p = create_prompt(project=self.project, name="P", content="v1 text")
        v1 = p.latest
        # Same content → no new version.
        same, created = save_version(p, content="v1 text")
        self.assertEqual((same.pk, created), (v1.pk, False))
        # Different content → v2.
        v2, created = save_version(p, content="v2 text", note="Tightened tone", user=self.user)
        self.assertTrue(created)
        self.assertEqual((v2.version, v2.content, v2.note), (2, "v2 text", "Tightened tone"))
        self.assertEqual(p.latest.version, 2)

    def test_unique_name_appends_counter(self):
        create_prompt(project=self.project, name="X", content="a")
        self.assertEqual(unique_name(self.project, "X"), "X (2)")
        create_prompt(project=self.project, name="X (2)", content="b")
        self.assertEqual(unique_name(self.project, "X"), "X (3)")

    def test_delete_version_blocked_when_only_one(self):
        p = create_prompt(project=self.project, name="Solo", content="only")
        with self.assertRaises(ValueError, msg="only version"):
            delete_version(p.latest)

    def test_delete_version_removes_intermediate(self):
        p = create_prompt(project=self.project, name="D", content="one")
        save_version(p, content="two")
        save_version(p, content="three")
        mid = p.versions.get(version=2)
        delete_version(mid)
        numbers = sorted(p.versions.values_list("version", flat=True))
        self.assertEqual(numbers, [1, 3])   # gap kept; numbers not renumbered

    def _run_using(self, version):
        """A minimal AuditRun that pins ``version`` (no engine submission)."""
        from audits.models import AuditRun
        from judges.models import Judge, JudgeVersion

        conn = ModelConnectionFactory(project=self.project)
        target = RegisteredModelFactory(connection=conn, display_name="T")
        judge_model = RegisteredModelFactory(connection=conn, display_name="J")
        sset = ScenarioSetFactory(project=self.project, name="S")
        sver = ScenarioSetVersionFactory(scenario_set=sset, version=1)
        judge, _ = Judge.objects.get_or_create(project=self.project, name="Test judge")
        jv = JudgeVersion.objects.create(judge=judge, version=1, base="safety")
        return AuditRun.objects.create(
            project=self.project, name="R", scenario_set_version=sver, target_model=target,
            auditor_model=target, judge_version=jv, judge_model=judge_model,
            simpleaudit_version="0.2.1", git_commit="", system_prompt_version=version,
            target_config_snapshot={}, auditor_config_snapshot={}, judge_config_snapshot={},
            generation_parameters_snapshot={"system_prompt": version.content},
        )

    def test_delete_prompt_blocked_when_run_pins_it(self):
        p = create_prompt(project=self.project, name="Pinned", content="used text")
        self._run_using(p.latest)
        with self.assertRaises(ValueError, msg="pinned"):
            delete_prompt(p)
        self.assertTrue(SystemPrompt.objects.filter(pk=p.pk).exists())

    def test_delete_prompt_blocked_when_agent_uses_its_text(self):
        p = create_prompt(project=self.project, name="Agent used", content="agent text")
        conn = ModelConnectionFactory(project=self.project)
        model = RegisteredModelFactory(connection=conn, display_name="M")
        AgentFactory(project=self.project, base_model=model, system_prompt="agent text")
        with self.assertRaises(ValueError, msg="agent"):
            delete_prompt(p)
        self.assertTrue(SystemPrompt.objects.filter(pk=p.pk).exists())

    def test_delete_prompt_allowed_when_unused(self):
        p = create_prompt(project=self.project, name="Unused", content="free text")
        delete_prompt(p)
        self.assertFalse(SystemPrompt.objects.filter(pk=p.pk).exists())


class PromptPageTests(_Base):
    def test_list_page(self):
        create_prompt(project=self.project, name="First", content="hello")
        resp = self.client.get("/prompts/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "First")

    def test_detail_page_shows_versions(self):
        p = create_prompt(project=self.project, name="V", content="one")
        save_version(p, content="two", note="second take")
        resp = self.client.get(f"/prompts/{p.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "v2")
        self.assertContains(resp, "second take")

    def test_post_creates_prompt(self):
        resp = self.client.post("/prompts/new/", {
            "name": "New One", "description": "desc", "content": "body", "note": "initial",
        })
        self.assertEqual(resp.status_code, 302)
        p = SystemPrompt.objects.get(project=self.project, name="New One")
        self.assertEqual(p.latest.content, "body")

    def test_post_edit_saves_new_version(self):
        p = create_prompt(project=self.project, name="E", content="before")
        resp = self.client.post(f"/prompts/{p.id}/", {
            "name": "E", "description": "", "content": "after", "note": "changed it",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(p.versions.count(), 2)
        self.assertEqual(p.latest.content, "after")

    def test_post_unchanged_content_no_new_version(self):
        p = create_prompt(project=self.project, name="U", content="same")
        self.client.post(f"/prompts/{p.id}/", {
            "name": "U", "description": "", "content": "same", "note": "",
        })
        self.assertEqual(p.versions.count(), 1)
