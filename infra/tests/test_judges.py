"""Judges: versioning, cloning, pages, run snapshots and the engine hand-off.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test infra.tests.test_judges
"""
from django.test import Client, TestCase

from infra.tests.factories import (
    AuditRunFactory,
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    ScenarioResultFactory,
    UserFactory,
)
from judges.models import Judge, JudgeVersion
from judges.services import (
    clone_judge,
    create_judge,
    ensure_starter_judges,
    judge_snapshot,
    rubric,
    save_version,
)


class _Base(TestCase):
    role = "admin"

    def setUp(self):
        self.project = ProjectFactory()
        conn = ModelConnectionFactory(project=self.project)
        self.m1 = RegisteredModelFactory(connection=conn, display_name="Mini")
        self.m2 = RegisteredModelFactory(connection=conn, display_name="Big")
        self.user = UserFactory()
        self.user.set_password("pw")
        self.user.save()
        MembershipFactory(user=self.user, project=self.project, role=self.role)
        self.client = Client()
        self.client.login(username=self.user.username, password="pw")


class JudgeServiceTests(_Base):
    def test_new_version_only_when_grading_setup_changes(self):
        judge = create_judge(project=self.project, name="Safety", model=self.m1, rubric="safety")
        v1 = judge.latest
        same, created = save_version(judge, model=self.m1, rubric="safety",
                                     judge_prompt=rubric("safety")["judge_prompt"])   # = rubric default
        self.assertEqual((same.pk, created), (v1.pk, False))
        v2, created = save_version(judge, model=self.m2, rubric="safety", note="Bigger model")
        self.assertTrue(created)
        self.assertEqual((v2.version, v2.model_id, v2.note), (2, self.m2.pk, "Bigger model"))
        v3, _ = save_version(judge, model=self.m2, rubric="safety", judge_prompt="Grade strictly.")
        self.assertEqual((v3.version, v3.judge_prompt), (3, "Grade strictly."))

    def test_prompts_equal_to_rubric_default_are_stored_blank(self):
        judge = create_judge(project=self.project, name="H", model=self.m1, rubric="helpfulness",
                             probe_prompt=rubric("helpfulness")["probe_prompt"])
        self.assertEqual(judge.latest.probe_prompt, "")

    def test_unknown_rubric_and_foreign_model_rejected(self):
        judge = create_judge(project=self.project, name="S", model=self.m1)
        with self.assertRaises(ValueError):
            save_version(judge, model=self.m1, rubric="nope")
        with self.assertRaises(ValueError):
            save_version(judge, model=RegisteredModelFactory(), rubric="safety")

    def test_clone_starts_a_new_history(self):
        judge = create_judge(project=self.project, name="Safety", model=self.m1, rubric="safety")
        save_version(judge, model=self.m1, rubric="safety", judge_prompt="Custom.")
        copy = clone_judge(judge.latest, user=self.user)
        self.assertEqual(copy.name, "Safety copy")
        self.assertEqual((copy.latest.version, copy.latest.judge_prompt), (1, "Custom."))
        self.assertEqual(clone_judge(judge.latest).name, "Safety copy (2)")

    def test_snapshot_resolves_prompts(self):
        judge = create_judge(project=self.project, name="F", model=self.m1, rubric="factuality", judge_prompt="Mine.")
        snap = judge_snapshot(judge.latest)
        self.assertEqual(snap["judge_prompt"], "Mine.")
        self.assertEqual(snap["probe_prompt"], rubric("factuality")["probe_prompt"])
        self.assertEqual((snap["custom_judge_prompt"], snap["custom_probe_prompt"], snap["output"]), (True, False, "score"))

    def test_starter_judges_are_idempotent(self):
        made = ensure_starter_judges(self.project, self.m1)
        self.assertIn("Safety", made)
        self.assertEqual(ensure_starter_judges(self.project, self.m1), [])
        self.assertEqual(Judge.objects.filter(project=self.project).count(), len(made))


class JudgePagesTests(_Base):
    def test_create_edit_and_view_versions(self):
        resp = self.client.post("/judges/new/", {
            "name": "Med safety", "description": "Clinical", "model_id": self.m1.id, "rubric": "safety",
            "probe_prompt": rubric("safety")["probe_prompt"], "judge_prompt": rubric("safety")["judge_prompt"],
        })
        judge = Judge.objects.get(name="Med safety")
        self.assertRedirects(resp, f"/judges/{judge.id}/", fetch_redirect_response=False)
        # Renaming alone does not make a version; changing the prompt does.
        base = {"name": "Medical safety", "description": "Clinical", "model_id": self.m1.id, "rubric": "safety",
                "probe_prompt": "", "judge_prompt": ""}
        self.client.post(f"/judges/{judge.id}/", base)
        judge.refresh_from_db()
        self.assertEqual((judge.name, judge.versions.count()), ("Medical safety", 1))
        self.client.post(f"/judges/{judge.id}/", {**base, "judge_prompt": "Be strict.", "note": "stricter"})
        self.assertEqual(judge.versions.count(), 2)
        page = self.client.get(f"/judges/{judge.id}/?v=1")
        self.assertEqual(page.context["shown"].version, 1)
        self.assertContains(page, "Viewing v1")
        self.assertContains(self.client.get(f"/judges/{judge.id}/"), "stricter")

    def test_duplicate_name_reshows_form(self):
        create_judge(project=self.project, name="Taken", model=self.m1)
        resp = self.client.post("/judges/new/", {"name": "Taken", "model_id": self.m1.id, "rubric": ""})
        self.assertContains(resp, "already exists")
        self.assertEqual(resp.context["form"]["name"], "Taken")

    def test_list_clone_and_delete_rules(self):
        used = create_judge(project=self.project, name="Used", model=self.m1)
        free = create_judge(project=self.project, name="Free", model=self.m1)
        AuditRunFactory(project=self.project, judge_model=self.m1, judge_version=used.latest)
        page = self.client.get("/judges/")
        self.assertContains(page, "Used")
        self.assertContains(page, "1 run")
        resp = self.client.post("/judges/", {"action": "clone", "version_id": used.latest.id})
        copy = Judge.objects.get(name="Used copy")
        self.assertRedirects(resp, f"/judges/{copy.id}/", fetch_redirect_response=False)
        self.client.post("/judges/", {"action": "delete", "judge_id": used.id})
        self.assertTrue(Judge.objects.filter(pk=used.pk).exists())   # runs use it
        self.client.post("/judges/", {"action": "delete", "judge_id": free.id})
        self.assertFalse(Judge.objects.filter(pk=free.pk).exists())

    def test_empty_state_creates_starters(self):
        page = self.client.get("/judges/")
        self.assertContains(page, "Create starter judges")
        self.client.post("/judges/", {"action": "starters", "model_id": self.m2.id})
        self.assertTrue(JudgeVersion.objects.filter(judge__project=self.project, model=self.m2, rubric="safety").exists())

    def test_other_workspace_judge_is_404(self):
        model = RegisteredModelFactory()
        other = create_judge(project=model.project, name="X", model=model)
        self.assertEqual(self.client.get(f"/judges/{other.id}/").status_code, 404)

    def test_model_used_by_a_judge_is_locked(self):
        create_judge(project=self.project, name="S", model=self.m2)
        from model_registry.services import model_usage_counts

        self.assertEqual(model_usage_counts(self.project).get(self.m2.id), 1)


class ViewerJudgeTests(_Base):
    role = "viewer"

    def test_viewer_cannot_change_judges(self):
        judge = create_judge(project=self.project, name="S", model=self.m1)
        self.client.post(f"/judges/{judge.id}/", {"name": "Hacked", "model_id": self.m1.id, "rubric": ""})
        self.client.post("/judges/", {"action": "delete", "judge_id": judge.id})
        judge.refresh_from_db()
        self.assertEqual(judge.name, "S")


class RunPagesShowJudgeTests(_Base):
    def test_run_detail_shows_judge_and_system_prompt(self):
        judge = create_judge(project=self.project, name="Helpful", model=self.m1, rubric="helpfulness")
        run = AuditRunFactory(
            project=self.project, judge_model=self.m1, judge_version=judge.latest,
            judge_config_snapshot={"model_id": "mini", "display_name": "Mini", "judge": judge_snapshot(judge.latest)},
            generation_parameters_snapshot={"system_prompt": "You are Acme's bot."},
        )
        page = self.client.get(f"/runs/{run.id}/")
        self.assertContains(page, "Helpful v1")
        self.assertContains(page, "Helpfulness")
        self.assertContains(page, "You are Acme&#x27;s bot.")

    def test_result_page_shows_score_rubric_grade(self):
        run = AuditRunFactory(project=self.project)
        judgment = {"score": 7.5, "relevance": 8, "accuracy": 7, "feedback": "Mostly right but vague about dosage."}
        result = ScenarioResultFactory(run_id=run.id, result={
            "severity": "low", "summary": "", "judgment": judgment, "conversation": [],
        })
        page = self.client.get(f"/runs/{run.id}/results/{result.id}/")
        self.assertContains(page, "Judge grade")
        self.assertContains(page, "7.5")
        self.assertContains(page, "Mostly right but vague")
