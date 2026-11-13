"""Expectation objects (a ``required`` flag per expected behaviour) and shared expectations on set versions."""
import importlib.util
import json
import sys
from unittest import mock

import pytest
from django.test import Client, TestCase
from rest_framework.test import APIClient

from accounts.models import Project, ProjectMembership, User
from audits.events import ScenarioResult, result_projection
from audits.services import shared_check_summary
from infra.engine import engine_expectations
from infra.hashing import scenario_set_version_hash
from infra.tests.factories import AuditRunFactory, ScenarioSetVersionItemFactory
from scenarios.expectations import (
    clean_expectations,
    expectation_display,
    expectation_rows,
    parse_expectation_form,
)
from scenarios.models import ScenarioRevision, ScenarioSet
from scenarios.services import (
    create_scenario,
    publish_scenario_set_version,
    set_shared_expectations,
    update_scenario_content,
    version_diff,
)

HAS_EXPECTATION_OBJECTS = importlib.util.find_spec("simpleaudit.expectations") is not None


# --- Stored form -------------------------------------------------------------


def test_required_items_without_id_are_stored_as_plain_strings():
    assert clean_expectations(["a", {"text": " b "}, {"text": "c", "required": True}]) == ["a", "b", "c"]


def test_flags_and_ids_are_kept_and_source_is_dropped():
    assert clean_expectations([
        {"text": "a", "required": False, "source": "shared"},
        {"id": "G1", "text": "b"},
    ]) == [{"text": "a", "required": False}, {"id": "G1", "text": "b", "required": True}]


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"text": ""}, "non-empty 'text'"),
        ({"text": "a", "required": "no"}, "'required' must be true or false"),
        ({"text": "a", "weight": 2}, "unknown expectation field"),
        (3, "must be a string or an object"),
    ],
)
def test_invalid_entries_are_refused(entry, message):
    with pytest.raises(ValueError, match=message):
        clean_expectations([entry])


def test_duplicate_ids_are_refused():
    with pytest.raises(ValueError, match="duplicate expectation id 'G1'"):
        clean_expectations([{"id": "G1", "text": "a"}, {"id": "G1", "text": "b"}])


def test_form_rows_win_over_lines_and_empty_rows_are_dropped():
    rows = json.dumps([{"id": "", "text": "a", "required": True}, {"id": "", "text": " ", "required": False},
                       {"id": "", "text": "b", "required": False}])
    assert parse_expectation_form(rows, "ignored") == ["a", {"text": "b", "required": False}]
    assert parse_expectation_form("", "x\n\n y \n") == ["x", "y"]


def test_rows_and_display_show_the_flag():
    entries = ["a", {"id": "G1", "text": "b", "required": False}]
    assert expectation_rows(entries)[1] == {"id": "G1", "text": "b", "required": False}
    assert expectation_display(entries) == ["a", "[G1] b (not counted)"]


# --- Engine bridge ------------------------------------------------------------


def test_engine_bridge_passes_plain_strings_unchanged():
    assert engine_expectations(["a", "b"]) == ["a", "b"]
    assert engine_expectations(None) is None


def test_engine_bridge_falls_back_to_texts_on_an_engine_without_objects():
    with mock.patch.dict(sys.modules, {"simpleaudit.expectations": None}):
        merged = engine_expectations([{"text": "own", "required": False}], [{"id": "G1", "text": "shared"}])
    assert merged == ["own", "shared"]


@pytest.mark.skipif(not HAS_EXPECTATION_OBJECTS, reason="installed SimpleAudit has no expectation objects")
def test_engine_bridge_merges_shared_rules_with_flags():
    merged = engine_expectations(["own"], [{"id": "G1", "text": "shared", "required": False}])
    assert [(m["text"], m["source"], m["required"]) for m in merged] == [
        ("own", "scenario", True), ("shared", "shared", False),
    ]


# --- Hashes -------------------------------------------------------------------


def test_set_hash_is_unchanged_without_shared_expectations():
    items = [{"position": 1, "scenario_id": 1, "scenario_key": "k", "revision": 1, "revision_content_hash": "h"}]
    assert scenario_set_version_hash(items) == scenario_set_version_hash(items, [])
    assert scenario_set_version_hash(items) != scenario_set_version_hash(items, ["rule"])


class ExpectationServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ef", password="pass12345")
        self.project = Project.objects.create(name="EF", slug="ef")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sset = ScenarioSet.objects.create(project=self.project, name="Set")
        self.scenario = create_scenario(project=self.project, user=self.user, key="s", title="S",
                                        description="d", expected_behavior=["a"])
        self.v1 = publish_scenario_set_version(scenario_set=self.sset, user=self.user, scenario_ids=[self.scenario.id])

    def test_changing_only_a_flag_creates_a_new_revision(self):
        first = self.scenario.revisions.get()
        rev = update_scenario_content(scenario=self.scenario, user=self.user, description="d",
                                      expected_behavior=[{"text": "a", "required": False}])
        assert rev.revision == 2
        assert rev.expected_behavior == [{"text": "a", "required": False}]
        assert rev.content_hash != first.content_hash
        same = update_scenario_content(scenario=self.scenario, user=self.user, description="d",
                                       expected_behavior=[{"text": "a", "required": False}])
        assert same.pk == rev.pk

    def test_shared_expectations_publish_a_version_and_carry_over(self):
        v2 = set_shared_expectations(scenario_set=self.sset, user=self.user,
                                     shared_expectations=[{"id": "G1", "text": "rule"}])
        assert v2.version == 2
        assert v2.shared_expectations == [{"id": "G1", "text": "rule", "required": True}]
        assert v2.content_hash != self.v1.content_hash
        assert set_shared_expectations(scenario_set=self.sset, user=self.user,
                                       shared_expectations=[{"id": "G1", "text": "rule"}]).pk == v2.pk
        # Editing a scenario keeps the set's shared rules.
        update_scenario_content(scenario=self.scenario, user=self.user, description="d2", expected_behavior=["a"])
        v3 = publish_scenario_set_version(scenario_set=self.sset, user=self.user, scenario_ids=[self.scenario.id])
        assert v3.shared_expectations == v2.shared_expectations
        diff = version_diff(self.v1, v2)
        assert diff["shared_expectations"] == {"from": [], "to": ["[G1] rule"]}


class ExpectationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="api", password="pass12345")
        self.project = Project.objects.create(name="API", slug="api")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client.force_authenticate(user=self.user)

    def test_api_accepts_objects_and_rejects_bad_flags(self):
        url = f"/api/projects/{self.project.id}/scenarios/create/"
        ok = self.client.post(url, {"title": "T", "description": "d", "expected_behavior": [
            "a", {"text": "b", "required": False}]}, format="json")
        assert ok.status_code == 201, ok.content
        assert ok.json()["latest_revision"]["expected_behavior"] == ["a", {"text": "b", "required": False}]
        bad = self.client.post(url, {"title": "U", "description": "d", "expected_behavior": [
            {"text": "b", "required": "no"}]}, format="json")
        assert bad.status_code == 400

    def test_export_uses_format_3_only_when_objects_are_used(self):
        url = f"/api/projects/{self.project.id}/scenarios/create/"
        self.client.post(url, {"key": "plain", "title": "P", "description": "d", "expected_behavior": ["a"]},
                         format="json")
        export = f"/api/projects/{self.project.id}/scenarios/export/"
        assert self.client.get(export).json()["format_version"] == 2
        self.client.post(url, {"key": "flag", "title": "F", "description": "d",
                               "expected_behavior": [{"text": "b", "required": False}]}, format="json")
        data = self.client.get(export).json()
        assert data["format_version"] == 3
        imported = self.client.post(f"/api/projects/{self.project.id}/scenarios/import/", data, format="json")
        assert imported.status_code in (200, 201), imported.content


class ExpectationWebTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="web", password="pass12345")
        self.project = Project.objects.create(name="Web", slug="web")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sset = ScenarioSet.objects.create(project=self.project, name="Set")
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_row_editor_posts_flags_and_set_page_shows_them(self):
        rows = json.dumps([{"id": "", "text": "Counts", "required": True},
                           {"id": "", "text": "Only reported", "required": False}])
        self.client.post("/scenarios/create/", {"name": "S", "description": "d", "set_id": self.sset.id,
                                                "expected_behavior_rows": rows})
        rev = ScenarioRevision.objects.get(scenario__project=self.project)
        assert rev.expected_behavior == ["Counts", {"text": "Only reported", "required": False}]
        page = self.client.get(f"/scenarios/?set={self.sset.id}")
        self.assertContains(page, "not counted")
        self.assertContains(page, "Shared expectations")

    def test_shared_expectations_form_publishes_a_new_version(self):
        self.client.post("/scenarios/create/", {"name": "S", "description": "d", "set_id": self.sset.id,
                                                "expected_behavior": "a"})
        rows = json.dumps([{"id": "G1", "text": "Never a dead end", "required": True}])
        self.client.post(f"/scenarios/shared/{self.sset.id}/", {"shared_expectations_rows": rows,
                                                                "base_version": 1})
        latest = self.sset.versions.order_by("-version").first()
        assert latest.version == 2
        assert latest.shared_expectations == [{"id": "G1", "text": "Never a dead end", "required": True}]
        stale = self.client.post(f"/scenarios/shared/{self.sset.id}/", {"shared_expectations_rows": "[]",
                                                                        "base_version": 1})
        assert stale.status_code == 302
        assert self.sset.versions.count() == 2

    def test_set_export_and_import_round_trip_flags_and_shared_rules(self):
        rows = json.dumps([{"id": "", "text": "Counts", "required": True},
                           {"id": "", "text": "Only reported", "required": False}])
        self.client.post("/scenarios/create/", {"name": "S", "description": "d", "set_id": self.sset.id,
                                                "expected_behavior_rows": rows})
        shared = json.dumps([{"id": "G1", "text": "Never a dead end", "required": False}])
        self.client.post(f"/scenarios/shared/{self.sset.id}/", {"shared_expectations_rows": shared})
        exported = self.client.get(f"/scenarios/{self.sset.id}/export/").json()
        assert exported["format_version"] == 3
        assert exported["shared_expectations"] == [{"id": "G1", "text": "Never a dead end", "required": False}]
        assert exported["scenarios"][0]["expected_behavior"] == ["Counts", {"text": "Only reported", "required": False}]

        target = ScenarioSet.objects.create(project=self.project, name="Copy")
        resp = self.client.post(f"/scenarios/{target.id}/import/", json.dumps(exported),
                                content_type="application/json")
        assert resp.status_code == 302, resp.content
        version = target.versions.order_by("-version").first()
        assert version.shared_expectations == exported["shared_expectations"]
        item = version.items.select_related("revision").get()
        assert item.revision.expected_behavior == ["Counts", {"text": "Only reported", "required": False}]

    def test_set_import_without_shared_rules_keeps_the_target_sets_rules(self):
        self.client.post("/scenarios/create/", {"name": "S", "description": "d", "set_id": self.sset.id,
                                                "expected_behavior": "a"})
        self.client.post(f"/scenarios/shared/{self.sset.id}/", {"shared_expectations_rows": json.dumps(
            [{"id": "G1", "text": "rule", "required": True}])})
        payload = {"format_version": 2, "scenarios": [{"key": "new", "title": "N", "description": "d",
                                                        "expected_behavior": ["b"]}]}
        resp = self.client.post(f"/scenarios/{self.sset.id}/import/", json.dumps(payload),
                                content_type="application/json")
        assert resp.status_code == 302, resp.content
        latest = self.sset.versions.order_by("-version").first()
        assert latest.shared_expectations == [{"id": "G1", "text": "rule", "required": True}]
        assert latest.scenario_count == 2

    def test_export_follows_the_viewed_version(self):
        self.client.post("/scenarios/create/", {"name": "S", "description": "d", "set_id": self.sset.id,
                                                "expected_behavior": "a"})
        self.client.post(f"/scenarios/shared/{self.sset.id}/", {"shared_expectations_rows": json.dumps(
            [{"id": "G1", "text": "rule", "required": True}])})
        old = self.client.get(f"/scenarios/{self.sset.id}/export/?version=1").json()
        assert (old["set_version"], old["format_version"], "shared_expectations" in old) == (1, 2, False)
        new = self.client.get(f"/scenarios/{self.sset.id}/export/").json()
        assert (new["set_version"], new["format_version"]) == (2, 3)
        assert self.client.get(f"/scenarios/{self.sset.id}/export/?version=9").status_code == 404

    def test_set_import_rejects_bad_shared_rules(self):
        payload = {"format_version": 3, "shared_expectations": [{"text": ""}], "scenarios": []}
        resp = self.client.post(f"/scenarios/{self.sset.id}/import/", json.dumps(payload),
                                content_type="application/json")
        assert resp.status_code == 400
        assert "Invalid shared expectations" in resp.json()["error"]


def _checklist(*items):
    return {"severity": "low", "judgment": {"checklist": list(items)}}


class SharedChecksTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sc", password="pw")
        self.run = AuditRunFactory()
        self.version = self.run.scenario_set_version
        self.version.shared_expectations = [{"id": "G1", "text": "No dead end", "required": True},
                                            {"id": "G9", "text": "Plain", "required": False}]
        self.version.save()
        self.item = ScenarioSetVersionItemFactory(version=self.version)

    def test_projection_keeps_only_shared_items(self):
        result = _checklist(
            {"id": "e-1", "expectation": "own", "source": "scenario", "status": "violated"},
            {"id": "G1", "expectation": "No dead end", "source": "shared", "required": True, "status": "met"},
        )
        assert result_projection(result)["shared_checks"] == [
            [{"id": "G1", "text": "No dead end", "required": True, "status": "met"}],
        ]
        assert "shared_checks" not in result_projection({"severity": "pass"})

    def test_summary_counts_per_rule_in_set_order(self):
        ScenarioResult.objects.create(run_id=self.run.id, version_item_id=str(self.item.pk), status="completed",
                                      result=_checklist(
            {"id": "G9", "expectation": "Plain", "source": "shared", "required": False, "status": "violated"},
            {"id": "G1", "expectation": "No dead end", "source": "shared", "required": True, "status": "violated"},
        ))
        ScenarioResult.objects.create(run_id=self.run.id, version_item_id="other", status="completed",
                                      result=_checklist(
            {"id": "G1", "expectation": "No dead end", "source": "shared", "required": True, "status": "met"},
        ))
        rows = shared_check_summary(self.run)
        assert [r["id"] for r in rows] == ["G1", "G9"]
        assert (rows[0]["violated"], rows[0]["met"], rows[0]["violated_pct"]) == (1, 1, 50)
        assert rows[0]["violated_items"] == [str(self.item.pk)]
        assert rows[1]["required"] is False

    def test_run_page_shows_the_shared_checks_panel(self):
        ProjectMembership.objects.create(project=self.run.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        ScenarioResult.objects.create(run_id=self.run.id, version_item_id=str(self.item.pk), status="completed",
                                      result=_checklist(
            {"id": "G1", "expectation": "No dead end", "source": "shared", "required": True, "status": "violated"},
        ))
        client = Client()
        client.force_login(self.user)
        session = client.session
        session["active_project_id"] = self.run.project.id
        session.save()
        page = client.get(f"/runs/{self.run.id}/")
        self.assertContains(page, "Shared checks")
        self.assertContains(page, "No dead end")
