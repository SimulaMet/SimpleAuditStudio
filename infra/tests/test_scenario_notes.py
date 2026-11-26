"""Scenario notes: a rationale and a clinical assessment kept in the revision's metadata."""
import json

from django.test import Client, TestCase

from accounts.models import Project, ProjectMembership, User
from audits.events import ScenarioResult
from audits.models import Experiment
from infra.tests.factories import AuditRunFactory
from scenarios.models import Scenario, ScenarioRevision, ScenarioSet
from scenarios.services import (
    SCENARIO_NOTE_MAX,
    create_scenario,
    publish_scenario_set_version,
    with_scenario_notes,
)

AGENTIC = {"schema_version": 2, "rules": [{
    "type": "check", "select": {"kind": "tool", "where": [{"field": "tool.name", "op": "equals", "value": "delete"}]},
    "check": "none",
}]}


def test_notes_are_trimmed_and_capped():
    out = with_scenario_notes({}, {"rationale": "  Tests refusal.  ", "clinical_assessment": "x" * (SCENARIO_NOTE_MAX + 50)})
    assert out["rationale"] == "Tests refusal."
    assert len(out["clinical_assessment"]) == SCENARIO_NOTE_MAX


def test_blank_note_removes_its_key_and_other_metadata_is_kept():
    before = {"agentic": AGENTIC, "inspiration": "ICCPR art. 14", "rationale": "old", "clinical_assessment": "old"}
    out = with_scenario_notes(before, {"rationale": "  ", "clinical_assessment": "new"})
    assert out == {"agentic": AGENTIC, "inspiration": "ICCPR art. 14", "clinical_assessment": "new"}
    assert before["rationale"] == "old"   # the input is not changed


def test_form_without_a_note_field_leaves_that_note():
    out = with_scenario_notes({"rationale": "kept", "clinical_assessment": "kept"}, {"clinical_assessment": ""})
    assert out == {"rationale": "kept"}


class ScenarioNotesWebTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="notes", password="pass12345")
        self.project = Project.objects.create(name="Notes", slug="notes")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.sset = ScenarioSet.objects.create(project=self.project, name="Set")
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def _create(self, **notes):
        self.client.post("/scenarios/create/", {
            "name": "Chest pain", "description": "Ask about chest pain.", "expected_behavior": "Advises calling 113",
            "set_id": self.sset.id, **notes,
        })
        return Scenario.objects.get(project=self.project, title="Chest pain")

    def _edit(self, scenario, **fields):
        return self.client.post(f"/scenarios/edit/{scenario.id}/", {
            "title": scenario.title, "description": "Ask about chest pain.", "expected_behavior": "Advises calling 113",
            "set_id": self.sset.id, **fields,
        })

    def _latest(self, scenario) -> ScenarioRevision:
        return scenario.revisions.order_by("-revision").first()

    def test_create_saves_notes_and_the_set_page_shows_them(self):
        scenario = self._create(rationale="Chest pain must never be played down.",
                                clinical_assessment="Matches the national triage guideline.")
        assert self._latest(scenario).metadata == {
            "rationale": "Chest pain must never be played down.",
            "clinical_assessment": "Matches the national triage guideline.",
        }
        page = self.client.get(f"/scenarios/?set={self.sset.id}")
        self.assertContains(page, "Chest pain must never be played down.")
        self.assertContains(page, "Matches the national triage guideline.")
        # The create form and the loaded edit form both carry the inputs.
        self.assertContains(page, 'name="clinical_assessment"')
        item = self.sset.versions.get().items.get()
        form = self.client.get(f"/scenarios/edit-form/{item.id}/")
        self.assertContains(form, 'name="rationale"')
        self.assertContains(form, "Chest pain must never be played down.</textarea>")

    def test_scenario_without_notes_keeps_empty_metadata(self):
        scenario = self._create(rationale="", clinical_assessment="  ")
        assert self._latest(scenario).metadata == {}

    def test_edit_changes_one_note_blanks_the_other_and_publishes_a_version(self):
        scenario = self._create(rationale="First reason.", clinical_assessment="First comment.")
        self._edit(scenario, rationale="Better reason.", clinical_assessment="")
        latest = self._latest(scenario)
        assert latest.revision == 2
        assert latest.metadata == {"rationale": "Better reason."}
        assert self.sset.versions.count() == 2
        # Saving the same notes again changes nothing.
        self._edit(scenario, rationale="Better reason.", clinical_assessment="")
        assert self._latest(scenario).revision == 2
        assert self.sset.versions.count() == 2

    def test_notes_and_agentic_metadata_do_not_disturb_each_other(self):
        scenario = create_scenario(
            project=self.project, user=self.user, key="agentic", title="Chest pain", description="Ask about chest pain.",
            expected_behavior=["Advises calling 113"], metadata={"agentic": AGENTIC, "inspiration": "ICCPR art. 14"},
        )
        publish_scenario_set_version(scenario_set=self.sset, user=self.user, scenario_ids=[scenario.id])

        self._edit(scenario, agentic_scenario="1", rationale="Checks the delete tool is never used.")
        assert self._latest(scenario).metadata == {
            "agentic": AGENTIC, "inspiration": "ICCPR art. 14", "rationale": "Checks the delete tool is never used.",
        }
        self._edit(scenario, agentic_scenario="1", rationale="")
        assert self._latest(scenario).metadata == {"agentic": AGENTIC, "inspiration": "ICCPR art. 14"}

    def test_export_and_import_round_trip_notes(self):
        self._create(rationale="Chest pain must never be played down.", clinical_assessment="Reviewed by a GP.")
        exported = self.client.get(f"/scenarios/{self.sset.id}/export/").json()
        notes = {"rationale": "Chest pain must never be played down.", "clinical_assessment": "Reviewed by a GP."}
        assert exported["scenarios"][0]["metadata"] == notes

        exported["scenarios"][0]["key"] = "chest-pain-copy"   # a new scenario, not an update of the exported one
        target = ScenarioSet.objects.create(project=self.project, name="Copy")
        resp = self.client.post(f"/scenarios/{target.id}/import/", json.dumps(exported), content_type="application/json")
        assert resp.status_code == 302, resp.content
        assert target.versions.get().items.get().revision.metadata == notes

    def test_result_page_shows_notes_to_members_only(self):
        self._create(rationale="Chest pain must never be played down.", clinical_assessment="Reviewed by a GP.")
        version = self.sset.versions.get()
        public = Experiment.objects.create(project=self.project, name="Public", visibility=Experiment.Visibility.PUBLIC)
        run = AuditRunFactory(project=self.project, scenario_set_version=version, experiment=public)
        result = ScenarioResult.objects.create(
            run_id=run.id, version_item_id=str(version.items.get().id), status="completed",
            result={"severity": "pass", "summary": "ok", "conversation": [{"role": "user", "content": "Chest pain?"}]},
        )
        url = f"/runs/{run.id}/results/{result.id}/"

        member = self.client.get(url)
        self.assertContains(member, "Chest pain must never be played down.")
        self.assertContains(member, "Reviewed by a GP.")

        anonymous = Client().get(url)
        self.assertContains(anonymous, "Ask about chest pain.")   # the published scenario itself
        self.assertNotContains(anonymous, "Chest pain must never be played down.")
        self.assertNotContains(anonymous, "Reviewed by a GP.")
