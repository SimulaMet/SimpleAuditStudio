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
    base,
    create_judge,
    ensure_starter_judges,
    judge_snapshot,
    make_spec,
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
    def test_new_version_only_when_grading_changes(self):
        judge = create_judge(project=self.project, name="Safety", base="safety")
        v1 = judge.latest
        same, created = save_version(judge, base="safety", criteria=base("safety")["criteria"])   # = SimpleAudit's
        self.assertEqual((same.pk, created), (v1.pk, False))
        v2, created = save_version(judge, base="safety", criteria="Grade strictly.", note="Stricter")
        self.assertTrue(created)
        self.assertEqual((v2.version, v2.criteria, v2.note), (2, "Grade strictly.", "Stricter"))
        v3, _ = save_version(judge, base="harm")
        self.assertEqual((v3.version, v3.base, v3.output), (3, "harm", "severity"))

    def test_text_equal_to_the_base_is_stored_blank(self):
        judge = create_judge(project=self.project, name="H", base="helpfulness",
                             probe_prompt=base("helpfulness")["probe_prompt"] + "\n")
        self.assertEqual((judge.latest.probe_prompt, judge.latest.criteria, judge.latest.output), ("", "", "score"))

    def test_base_brings_its_format(self):
        spec = make_spec(base="helpfulness", output="binary", options={"question": "ignored?"})
        self.assertEqual((spec["output"], spec["options"]), ("score", {}))

    def test_own_formats(self):
        spec = make_spec(output="score", criteria="Rate the teaching.", options={"dimensions": "Accuracy, Clarity\n"})
        self.assertEqual(spec["options"], {"dimensions": ["Accuracy", "Clarity"]})
        spec = make_spec(output="binary", criteria="Quoting counts.",
                         options={"question": " Did it leak? ", "pass_when": "no"})
        self.assertEqual(spec["options"], {"question": "Did it leak?", "pass_when": False})
        self.assertEqual(make_spec(output="severity", criteria="Tone.", options={"dimensions": ["x"]})["options"], {})

    def test_invalid_specs_rejected(self):
        for kwargs, message in [
            ({"base": "nope"}, "Unknown SimpleAudit judge"),
            ({"output": "stars", "criteria": "x"}, "output format"),
            ({"output": "severity"}, "criteria"),
            ({"output": "binary", "criteria": "x"}, "question"),
            ({"output": "score", "criteria": "x", "options": {"dimensions": ["Tone", "tone"]}}, "distinct"),
            ({"output": "score", "criteria": "x", "options": {"dimensions": [f"d{i}" for i in range(11)]}}, "at most"),
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaisesMessage(ValueError, message):
                make_spec(**kwargs)

    def test_snapshot_resolves_texts(self):
        judge = create_judge(project=self.project, name="F", base="factuality", criteria="Mine.")
        snap = judge_snapshot(judge.latest)
        self.assertTrue(snap["judge_prompt"].startswith("Mine.\n\n"))
        self.assertTrue(snap["judge_prompt"].endswith(base("factuality")["format_prompt"]))
        self.assertEqual(snap["probe_prompt"], base("factuality")["probe_prompt"])
        self.assertEqual((snap["custom_criteria"], snap["custom_probe_prompt"], snap["output"]), (True, False, "score"))
        self.assertEqual(snap["spec"], judge.latest.content())

    def test_own_score_snapshot(self):
        judge = create_judge(project=self.project, name="Teaching", output="score", criteria="Rate the teaching.",
                             options={"dimensions": ["Accuracy", "Tone & style"]})
        snap = judge_snapshot(judge.latest)
        self.assertEqual((snap["base_name"], snap["output_label"]), ("", "Score 1–10"))
        self.assertIn('"tone_style": <integer 1-10>', snap["judge_prompt"])

    def test_starter_judges_mirror_the_library(self):
        from simpleaudit.judges import JUDGE_CONFIGS

        Judge.objects.filter(project=self.project).delete()   # new workspaces already have them
        made = ensure_starter_judges(self.project)
        self.assertIn("Safety Judge (SimpleAudit Default)", made)
        self.assertIn(JUDGE_CONFIGS["harm"]["name"], made)   # library names, verbatim
        self.assertNotIn("SimpleAudit Default", made)
        self.assertEqual(len(made), len(JUDGE_CONFIGS))
        self.assertEqual(ensure_starter_judges(self.project), [])

    def test_default_judge_is_the_safety_judge(self):
        from judges.services import default_judge_version

        Judge.objects.filter(project=self.project).delete()
        version = default_judge_version(self.project)
        self.assertEqual((version.base, version.judge.name), ("safety", "Safety Judge (SimpleAudit Default)"))
        self.assertEqual(default_judge_version(self.project).pk, version.pk)

    def test_unnamed_default_only_offered_to_versions_using_it(self):
        from judges.services import base_choices

        self.assertNotIn("default", [b["key"] for b in base_choices()])
        self.assertIn("default", [b["key"] for b in base_choices("default")])


class JudgePagesTests(_Base):
    def test_create_edit_and_view_versions(self):
        resp = self.client.post("/judges/new/", {
            "name": "Med safety", "description": "Clinical", "start": "safety",
            "probe_prompt": base("safety")["probe_prompt"], "criteria": base("safety")["criteria"],
        })
        judge = Judge.objects.get(name="Med safety")
        self.assertRedirects(resp, f"/judges/{judge.id}/", fetch_redirect_response=False)
        # Renaming alone does not make a version; changing the criteria does.
        form = {"name": "Medical safety", "description": "Clinical", "start": "safety",
                "probe_prompt": "", "criteria": ""}
        self.client.post(f"/judges/{judge.id}/", form)
        judge.refresh_from_db()
        self.assertEqual((judge.name, judge.versions.count()), ("Medical safety", 1))
        self.client.post(f"/judges/{judge.id}/", {**form, "criteria": "Be strict.", "note": "stricter"})
        self.assertEqual(judge.versions.count(), 2)
        page = self.client.get(f"/judges/{judge.id}/?v=1")
        self.assertEqual(page.context["shown"].version, 1)
        self.assertContains(page, "Viewing v1")
        self.assertContains(self.client.get(f"/judges/{judge.id}/"), "stricter")

    def test_create_own_criteria_judges(self):
        self.client.post("/judges/new/", {
            "name": "Teaching", "start": "format:score", "criteria": "Rate how well it teaches.",
            "dimensions": "Accuracy\nClarity", "probe_prompt": "", "question": "", "pass_when": "yes",
        })
        v = Judge.objects.get(name="Teaching").latest
        self.assertEqual((v.base, v.output, v.options), ("", "score", {"dimensions": ["Accuracy", "Clarity"]}))
        self.client.post("/judges/new/", {
            "name": "Leak", "start": "format:binary", "criteria": "Quoting counts.",
            "question": "Did it reveal its system prompt?", "pass_when": "no", "dimensions": "Ignored",
        })
        v = Judge.objects.get(name="Leak").latest
        self.assertEqual(v.options, {"question": "Did it reveal its system prompt?", "pass_when": False})
        page = self.client.get(f"/judges/{v.judge_id}/")
        self.assertEqual((page.context["form"]["start"], page.context["form"]["pass_when"]), ("format:binary", "no"))
        self.assertContains(page, "Own criteria · Yes / no")

    def test_invalid_own_judge_reshows_form(self):
        resp = self.client.post("/judges/new/", {"name": "Empty", "start": "format:severity", "criteria": " "})
        self.assertContains(resp, "Write the criteria")
        self.assertEqual(resp.context["form"]["start"], "format:severity")
        self.assertFalse(Judge.objects.filter(name="Empty").exists())

    def test_invalid_edit_keeps_the_old_name(self):
        judge = create_judge(project=self.project, name="Keep")
        self.client.post(f"/judges/{judge.id}/", {"name": "Renamed", "start": "format:binary", "criteria": "x"})
        judge.refresh_from_db()
        self.assertEqual((judge.name, judge.versions.count()), ("Keep", 1))

    def test_new_judge_from_query(self):
        page = self.client.get("/judges/new/?base=harm")
        self.assertEqual((page.context["form"]["start"], page.context["form"]["criteria"]),
                         ("harm", base("harm")["criteria"]))
        self.assertEqual(self.client.get("/judges/new/?format=score").context["form"]["start"], "format:score")

    def test_preview_builds_the_full_prompt(self):
        out = self.client.post("/judges/preview/", {"start": "harm", "criteria": "Only fraud."}).json()
        self.assertTrue(out["judge_prompt"].startswith("Only fraud.\n\n"))
        self.assertIn('"category"', out["judge_prompt"])
        out = self.client.post("/judges/preview/", {"start": "format:binary", "criteria": "x", "question": ""}).json()
        self.assertIn("question", out["error"])

    def test_clone_prefills_without_saving(self):
        judge = create_judge(project=self.project, name="Harm", base="harm", description="HELM")
        save_version(judge, base="harm", criteria="Only fraud.", probe_prompt="Ask about money.")
        before = Judge.objects.count()
        page = self.client.get(f"/judges/new/?clone={judge.latest.id}")
        form = page.context["form"]
        self.assertEqual((form["name"], form["description"], form["start"]), ("Harm copy", "HELM", "harm"))
        self.assertEqual((form["criteria"], form["probe_prompt"]), ("Only fraud.", "Ask about money."))
        self.assertContains(page, "Nothing is saved until")
        self.assertFalse(Judge.objects.filter(name="Harm copy").exists())
        # Submitting creates it, with its own history and a note on where it came from.
        self.client.post("/judges/new/", {**form, "criteria": "Only phishing."})
        copy = Judge.objects.get(name="Harm copy")
        self.assertEqual((copy.latest.version, copy.latest.criteria, copy.latest.note),
                         (1, "Only phishing.", "Cloned from Harm v2"))
        self.assertEqual(self.client.get(f"/judges/new/?clone={judge.latest.id}").context["form"]["name"],
                         "Harm copy (2)")
        self.assertEqual(Judge.objects.count(), before + 1)

    def test_cloning_a_simpleaudit_judge_becomes_own_criteria_when_a_format_matches(self):
        safety = create_judge(project=self.project, name="Safe", base="safety")
        form = self.client.get(f"/judges/new/?clone={safety.latest.id}").context["form"]
        self.assertEqual(form["start"], "format:severity")
        self.assertIn("MANIPULATION RESISTANCE", form["criteria"])
        self.assertNotIn("SEVERITY LEVELS", form["criteria"])   # the format adds the ladder
        self.client.post("/judges/new/", form)
        v = Judge.objects.get(name="Safe copy").latest
        self.assertEqual((v.base, v.output), ("", "severity"))
        # Same grading: the composed prompt still has the ladder once.
        self.assertEqual(judge_snapshot(v)["judge_prompt"].count("SEVERITY LEVELS"), 1)

        helpful = create_judge(project=self.project, name="Help", base="helpfulness")
        form = self.client.get(f"/judges/new/?clone={helpful.latest.id}").context["form"]
        self.assertEqual((form["start"], form["dimensions"]),
                         ("format:score", "Relevance\nAccuracy\nClarity\nCompleteness"))
        self.assertNotIn("OVERALL SCORE", form["criteria"])

    def test_cloning_a_judge_with_extra_fields_keeps_its_format(self):
        harm = create_judge(project=self.project, name="H", base="harm")
        page = self.client.get(f"/judges/new/?clone={harm.latest.id}")
        self.assertEqual(page.context["form"]["start"], "harm")
        self.assertContains(page, "keeps its output format")

    def test_clone_of_other_workspace_is_ignored(self):
        other = create_judge(project=ProjectFactory(), name="X")
        page = self.client.get(f"/judges/new/?clone={other.latest.id}")
        self.assertIsNone(page.context["cloning"])
        self.assertEqual(page.context["form"]["name"], "")

    def test_versions_can_be_compared(self):
        judge = create_judge(project=self.project, name="S", base="safety")
        save_version(judge, base="safety", criteria="Be strict.", note="stricter")
        page = self.client.get(f"/judges/{judge.id}/")
        self.assertEqual(page.context["previous"].version, 1)
        self.assertContains(page, "Changes from v1")
        v2, v1 = page.context["diff_versions"]
        fields = lambda v: {f["label"]: f["value"] for f in v["fields"]}
        self.assertEqual((fields(v2)["Criteria"], v2["note"]), ("Be strict.", "stricter"))
        self.assertIn("MANIPULATION RESISTANCE", fields(v1)["Criteria"])
        self.assertIsNone(self.client.get(f"/judges/{judge.id}/?v=1").context["previous"])

    def test_delete_unused_versions_only(self):
        judge = create_judge(project=self.project, name="S", base="safety")
        v2, _ = save_version(judge, base="safety", criteria="Two.")
        save_version(judge, base="safety", criteria="Three.")
        AuditRunFactory(project=self.project, judge_model=self.m1, judge_version=judge.versions.get(version=1))
        page = self.client.get(f"/judges/{judge.id}/")
        self.assertContains(page, "Delete v2")
        self.assertNotContains(page, "Delete v1")   # a run uses it
        self.client.post("/judges/", {"action": "delete_version", "version_id": judge.versions.get(version=1).id})
        self.client.post("/judges/", {"action": "delete_version", "version_id": v2.id})
        self.assertEqual(sorted(judge.versions.values_list("version", flat=True)), [1, 3])
        # Other versions keep their numbers; "Changes" compares with the previous remaining one.
        v4, _ = save_version(judge, base="safety", criteria="Four.")
        self.assertEqual(v4.version, 4)
        self.assertEqual(self.client.get(f"/judges/{judge.id}/?v=3").context["previous"].version, 1)

    def test_judge_page_delete(self):
        free = create_judge(project=self.project, name="Free")
        used = create_judge(project=self.project, name="Used")
        AuditRunFactory(project=self.project, judge_model=self.m1, judge_version=used.latest)
        self.assertContains(self.client.get(f"/judges/{free.id}/"), 'value="delete"')
        self.assertContains(self.client.get(f"/judges/{used.id}/"), "Can't delete: runs or monitors use this judge")
        self.assertRedirects(self.client.post("/judges/", {"action": "delete", "judge_id": free.id}), "/judges/",
                             fetch_redirect_response=False)
        self.assertFalse(Judge.objects.filter(pk=free.pk).exists())

    def test_only_version_cannot_be_deleted(self):
        judge = create_judge(project=self.project, name="Solo")
        resp = self.client.post("/judges/", {"action": "delete_version", "version_id": judge.latest.id}, follow=True)
        self.assertContains(resp, "delete the judge instead")
        self.assertEqual(judge.versions.count(), 1)

    def test_duplicate_name_reshows_form(self):
        create_judge(project=self.project, name="Taken")
        resp = self.client.post("/judges/new/", {"name": "Taken", "start": "safety"})
        self.assertContains(resp, "already exists")
        self.assertEqual(resp.context["form"]["name"], "Taken")

    def test_list_clone_and_delete_rules(self):
        used = create_judge(project=self.project, name="Used")
        free = create_judge(project=self.project, name="Free")
        AuditRunFactory(project=self.project, judge_model=self.m1, judge_version=used.latest)
        page = self.client.get("/judges/")
        self.assertContains(page, "Used")
        self.assertContains(page, "1 run")
        self.client.post("/judges/", {"action": "delete", "judge_id": used.id})
        self.assertTrue(Judge.objects.filter(pk=used.pk).exists())   # runs use it
        self.client.post("/judges/", {"action": "delete", "judge_id": free.id})
        self.assertFalse(Judge.objects.filter(pk=free.pk).exists())

    def test_new_workspace_has_simpleaudits_judges(self):
        from simpleaudit.judges import JUDGE_CONFIGS

        from accounts.services import create_workspace

        project = create_workspace(user=self.user, name="Fresh")
        bases = set(JudgeVersion.objects.filter(judge__project=project).values_list("base", flat=True))
        self.assertEqual(bases, set(JUDGE_CONFIGS))
        self.assertTrue(Judge.objects.filter(project=project, name="Safety Judge (SimpleAudit Default)").exists())
        # ...and the standard scenario sets, published.
        from infra.seed import DEFAULT_PACKS
        from scenarios.models import ScenarioSet

        sets = ScenarioSet.objects.filter(project=project, versions__isnull=False).distinct()
        self.assertEqual(sets.count(), len(DEFAULT_PACKS))

    def test_missing_library_judges_are_offered(self):
        ensure_starter_judges(self.project)
        Judge.objects.filter(project=self.project, versions__base="harm").delete()   # e.g. added upstream later
        page = self.client.get("/judges/")
        self.assertContains(page, "doesn't have yet")
        self.client.post("/judges/", {"action": "starters"})
        self.assertTrue(JudgeVersion.objects.filter(judge__project=self.project, base="harm").exists())
        self.assertNotContains(self.client.get("/judges/"), "doesn't have yet")

    def test_empty_state_creates_starters(self):
        Judge.objects.filter(project=self.project).delete()
        page = self.client.get("/judges/")
        self.assertContains(page, "Add SimpleAudit's judges")
        self.client.post("/judges/", {"action": "starters"})
        self.assertTrue(JudgeVersion.objects.filter(judge__project=self.project, base="safety").exists())

    def test_other_workspace_judge_is_404(self):
        other = create_judge(project=ProjectFactory(), name="X")
        self.assertEqual(self.client.get(f"/judges/{other.id}/").status_code, 404)

    def test_judge_pages_have_no_model_picker(self):
        judge = create_judge(project=self.project, name="S")
        self.assertNotContains(self.client.get(f"/judges/{judge.id}/"), 'name="model_id"')


class ViewerJudgeTests(_Base):
    role = "viewer"

    def test_viewer_can_preview(self):
        out = self.client.post("/judges/preview/", {"start": "safety"}).json()
        self.assertIn("SEVERITY LEVELS", out["judge_prompt"])

    def test_viewer_cannot_change_judges(self):
        judge = create_judge(project=self.project, name="S")
        self.client.post(f"/judges/{judge.id}/", {"name": "Hacked", "start": "safety"})
        self.client.post("/judges/", {"action": "delete", "judge_id": judge.id})
        judge.refresh_from_db()
        self.assertEqual(judge.name, "S")


class RunPagesShowJudgeTests(_Base):
    def test_run_detail_shows_judge_and_system_prompt(self):
        judge = create_judge(project=self.project, name="Helpful", base="helpfulness")
        run = AuditRunFactory(
            project=self.project, judge_model=self.m1, judge_version=judge.latest,
            judge_config_snapshot={"model_id": "mini", "display_name": "Mini", "judge": judge_snapshot(judge.latest)},
            generation_parameters_snapshot={"system_prompt": "You are Acme's bot."},
        )
        page = self.client.get(f"/runs/{run.id}/")
        self.assertContains(page, "Helpful v1")
        self.assertContains(page, "Helpfulness")
        self.assertContains(page, "You are Acme&#x27;s bot.")

    def test_result_page_shows_score_grade(self):
        run = AuditRunFactory(project=self.project)
        judgment = {"score": 7.5, "relevance": 8, "accuracy": 7, "feedback": "Mostly right but vague about dosage."}
        result = ScenarioResultFactory(run_id=run.id, result={
            "severity": "low", "summary": "", "judgment": judgment, "conversation": [],
        })
        page = self.client.get(f"/runs/{run.id}/results/{result.id}/")
        self.assertContains(page, "Judge grade")
        self.assertContains(page, "7.5")
        self.assertContains(page, "Mostly right but vague")


class BaseCatalogueTests(TestCase):
    def test_bases_mirror_simpleaudit(self):
        from simpleaudit.judges import DEFAULT_JUDGE, JUDGE_CONFIGS

        from judges.services import bases

        self.assertEqual(base("binary_abstention")["output"], "binary")
        self.assertEqual(base("helpfulness")["output"], "score")
        self.assertEqual(base("harm")["fields"], list(JUDGE_CONFIGS["harm"]["response_schema"]["properties"]))
        self.assertEqual(bases()["default"]["criteria"], DEFAULT_JUDGE["criteria"].strip())


class UngradedTrialsTests(TestCase):
    def test_ungraded_is_neither_pass_nor_fail(self):
        from audits.monitors import pass_counts

        run = AuditRunFactory()
        for i, sev in enumerate(("pass", "high", "ungraded", "ungraded")):
            ScenarioResultFactory(run_id=run.id, version_item_id=str(i), status="completed", result={"severity": sev})
        self.assertEqual(pass_counts([run.id])[run.id], {"k": 1, "n": 2, "errors": 0, "ungraded": 2})
