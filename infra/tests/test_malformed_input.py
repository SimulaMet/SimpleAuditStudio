"""Malformed query/form/JSON input must yield a client error or a normal page, never a 500."""
from unittest import mock

from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from scenarios.models import Scenario


class MalformedInputTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pa", password="pass12345")
        self.project = Project.objects.create(name="P", slug="p")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        self.client.raise_request_exception = False

    def assertNot500(self, resp, label):
        self.assertLess(resp.status_code, 500, label)

    def test_unicode_digit_query_params(self):
        # "²".isdigit() is True but int("²") raises.
        for url in ["/scenarios/?set=%C2%B2", "/workspaces/?manage=%C2%B2", "/experiments/new/?clone_from=%C2%B2",
                    "/compare/?runs=%C2%B2,1", "/runs/data/?target=%C2%B2", "/runs/data/?q=%23%C2%B2",
                    "/runs/export.csv?set=%C2%B2"]:
            self.assertNot500(self.client.get(url), url)
        self.assertNot500(self.client.post("/compare/", {"runs[]": ["²", "1"]}), "compare post")

    def test_bulk_and_preference_bodies(self):
        for body in ["[1]", '{"ids":[1e400],"action":"archive"}']:
            self.assertNot500(self.client.post("/runs/bulk/", body, content_type="application/json"), body)
        for body in ["[]", '{"key":[1]}']:
            self.assertNot500(self.client.post("/me/preferences/", body, content_type="application/json"), body)

    def test_scenario_forms_with_bad_set_id(self):
        self.assertNot500(self.client.post("/scenarios/create/", {"name": "X", "set_id": "abc"}), "create")
        scenario = Scenario.objects.first()
        self.assertIsNotNone(scenario)
        self.assertNot500(self.client.post(
            f"/scenarios/edit/{scenario.id}/", {"title": "Y", "description": "changed", "set_id": "abc"}), "edit")
        self.assertNot500(self.client.post(f"/scenarios/delete/{scenario.id}/", {"set_id": "abc"}), "delete")

    def test_connection_forms_with_bad_ids(self):
        for data in [
            {"action": "edit_connection", "conn_id": "abc", "conn_name": "n", "conn_base_url": "https://x.example/v1"},
            {"action": "add_models", "conn_id": "abc"},
            {"action": "edit_model", "rm_id": "abc"},
        ]:
            self.assertNot500(self.client.post("/connections/", data), data["action"])
        self.assertNot500(self.client.post("/connections/discover/", {"connection_id": "abc"}), "discover")

    def test_agent_form_with_bad_base_model(self):
        with mock.patch("infra.ui.chat_enabled", return_value=True):
            self.assertNot500(self.client.post("/agents/new/", {"name": "A", "base_model": "abc"}), "abc")
            self.assertNot500(self.client.post("/agents/new/", {"name": "A"}), "missing")

    def test_oversized_numeric_ids(self):
        huge, long = "9" * 25, "9" * 5000
        for url in [f"/scenarios/?set={long}", f"/workspaces/?manage={long}", f"/runs/data/?q={long}",
                    f"/runs/data/?target={huge}", f"/runs/export.csv?set={huge}", f"/compare/?runs={huge},1"]:
            self.assertNot500(self.client.get(url), url[:40])
        body = '{"ids":[9999999999999999999999999],"action":"archive"}'
        self.assertNot500(self.client.post("/runs/bulk/", body, content_type="application/json"), "bulk huge")


class MalformedApiInputTest(TestCase):
    def setUp(self):
        self.client.raise_request_exception = False

    def test_token_endpoint_bad_bodies(self):
        for body in ["[1]", '{"username":"x","password":[1]}']:
            resp = self.client.post("/api/auth/token/", body, content_type="application/json")
            self.assertLess(resp.status_code, 500, body)

    def test_switch_workspace_bad_body(self):
        user = User.objects.create_user(username="sw", password="pass12345")
        self.client.force_login(user)
        resp = self.client.post("/api/projects/switch/", "[1]", content_type="application/json")
        self.assertLess(resp.status_code, 500)

    def test_agent_api_not_found_is_404(self):
        user = User.objects.create_user(username="ag", password="pass12345")
        project = Project.objects.create(name="AG", slug="ag")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = project.id
        session.save()
        for url in ["/api/agents/999999/", "/api/knowledge-bases/999999/", "/api/tools/999999/"]:
            self.assertEqual(self.client.get(url).status_code, 404, url)


class Pass14InputTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="p14", password="pass12345")
        self.project = Project.objects.create(name="P14", slug="p14")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        self.client.raise_request_exception = False

    def test_end_of_calendar_date_filter(self):
        for url in ["/runs/data/?to=9999-12-31", "/runs/export.csv?to=9999-12-31"]:
            self.assertLess(self.client.get(url).status_code, 500, url)

    def test_oversized_ids_in_audit_api(self):
        huge = "9" * 25
        resp = self.client.get(f"/api/projects/{self.project.id}/audit-runs/compare/?run_ids={huge},1")
        self.assertLess(resp.status_code, 500)

    def test_publish_api_with_duplicate_ids(self):
        from scenarios.models import ScenarioSet
        from scenarios.services import create_scenario, publish_scenario_set_version

        sset = ScenarioSet.objects.create(project=self.project, name="S")
        sc = create_scenario(project=self.project, user=self.user, key="k", title="T", description="d",
                             expected_behavior=["x"])
        version = publish_scenario_set_version(scenario_set=sset, user=self.user, scenario_ids=[sc.id, sc.id])
        self.assertEqual(version.items.count(), 1)

    def test_agent_monitor_refused(self):
        from audits.monitors import create_monitor

        with self.assertRaises(ValueError):
            create_monitor(project=self.project, user=self.user, name="m", run={"agent": object()},
                           repeat={"start": "now", "every": "day"})


class ApiBoundaryValidationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ab", password="pass12345")
        self.project = Project.objects.create(name="AB", slug="ab-api")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        self.client.raise_request_exception = False

    def _post(self, url, body):
        import json

        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_import_validates_items(self):
        from scenarios.models import Scenario

        resp = self._post(f"/api/projects/{self.project.id}/scenarios/import/", {"scenarios": [
            {"key": "bad", "title": "T", "description": "d", "expected_behavior": "not a list"},
            {"key": "ok", "title": "T", "description": "d", "expected_behavior": ["x"]},
        ]})
        self.assertLess(resp.status_code, 500)
        self.assertTrue(Scenario.objects.filter(project=self.project, key="ok").exists())
        self.assertFalse(Scenario.objects.filter(project=self.project, key="bad").exists())

    def test_bad_agentic_metadata_is_400(self):
        resp = self._post(f"/api/projects/{self.project.id}/scenarios/create/", {
            "title": "T", "description": "d", "expected_behavior": ["x"], "metadata": {"agentic": {"bogus": 1}}})
        self.assertEqual(resp.status_code, 400)

    def test_long_title_without_key_gets_capped_key(self):
        from scenarios.models import Scenario

        resp = self._post(f"/api/projects/{self.project.id}/scenarios/create/", {
            "title": "t" * 290, "description": "d", "expected_behavior": ["x"]})
        self.assertLess(resp.status_code, 500)
        self.assertLessEqual(len(Scenario.objects.get(project=self.project).key), 200)

    def test_agent_retrieval_settings_must_be_object(self):
        from infra.tests.factories import RegisteredModelFactory

        model = RegisteredModelFactory(project=self.project)
        resp = self._post("/api/agents/", {"name": "A", "base_model": model.id, "retrieval_settings": [1]})
        self.assertEqual(resp.status_code, 400)


class FieldLengthTest(TestCase):
    """Values beyond model max_length must be capped or refused (Postgres rejects them)."""

    def setUp(self):
        self.user = User.objects.create_user(username="fl", password="pass12345")
        self.project = Project.objects.create(name="FL", slug="fl")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_scenario_forms_cap_lengths(self):
        from scenarios.models import Scenario, ScenarioSet

        self.client.post("/scenarios/set-create/", {"name": "s" * 400})
        sset = ScenarioSet.objects.get(project=self.project)
        self.assertLessEqual(len(sset.name), 250)
        self.client.post("/scenarios/create/", {"name": "t" * 400, "category": "c" * 200,
                                                "set_id": str(sset.id), "description": "d"})
        self.assertTrue(Scenario.objects.filter(project=self.project).exists())
        for s in Scenario.objects.filter(project=self.project):
            self.assertLessEqual(len(s.title), 300)
            self.assertLessEqual(len(s.category), 120)

    def test_connection_overlong_key_and_url_refused(self):
        from model_registry.models import ModelConnection

        self.client.post("/connections/", {"action": "add_connection", "conn_name": "n" * 300,
                                           "conn_base_url": "https://x.example/" + "a" * 250, "conn_provider": "openai"})
        self.client.post("/connections/", {"action": "add_connection", "conn_name": "ok",
                                           "conn_base_url": "https://x.example/v1", "conn_provider": "openai",
                                           "key_mode": "stored", "conn_api_key": "k" * 900})
        for c in ModelConnection.objects.filter(project=self.project):
            self.assertLessEqual(len(c.base_url), 200)
            self.assertLessEqual(len(c.api_key_direct), 500)
            self.assertLessEqual(len(c.name), 250)

    def test_register_overlong_username_refused(self):
        self.client.logout()
        self.client.post("/register/", {"username": "u" * 200, "password": "Str0ng-pass-123"})
        self.assertFalse(User.objects.filter(username="u" * 200).exists())


class ScenarioUiFormValidationTest(TestCase):
    def setUp(self):
        from scenarios.models import ScenarioSet

        self.user = User.objects.create_user(username="uv", password="pass12345")
        self.project = Project.objects.create(name="UV", slug="uv")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sset = ScenarioSet.objects.create(project=self.project, name="S")
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        self.client.raise_request_exception = False

    def test_ui_import_rejects_malformed_items(self):
        import json

        from scenarios.models import Scenario

        resp = self.client.post(f"/scenarios/{self.sset.id}/import/", json.dumps({"scenarios": [
            {"key": "bad", "title": "T", "description": "d", "expected_behavior": "abc"}]}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Scenario.objects.filter(project=self.project, key="bad").exists())

    def test_documents_must_be_a_list(self):
        from scenarios.models import Scenario

        self.client.post("/scenarios/create/", {"name": "D", "description": "d", "documents": '"policy.pdf"',
                                                "set_id": str(self.sset.id)})
        self.assertFalse(Scenario.objects.filter(project=self.project, title="D").exists())

    def test_duplicate_set_name_does_not_500(self):
        from scenarios.models import ScenarioSet

        resp = self.client.post("/scenarios/set-create/", {"name": "S"})
        self.assertLess(resp.status_code, 500)
        other = ScenarioSet.objects.create(project=self.project, name="Other")
        resp = self.client.post(f"/scenarios/set-rename/{other.id}/", {"name": "S"})
        self.assertLess(resp.status_code, 500)
        self.assertEqual(ScenarioSet.objects.filter(project=self.project, name="S").count(), 1)


class ScenarioRoundTripAndConnectionFieldsTest(TestCase):
    def setUp(self):
        from scenarios.models import ScenarioSet

        self.user = User.objects.create_user(username="rt", password="pass12345")
        self.project = Project.objects.create(name="RT", slug="rt")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sset = ScenarioSet.objects.create(project=self.project, name="S")
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_export_import_round_trip_with_string_documents(self):
        from scenarios.models import ScenarioSet

        self.client.post("/scenarios/create/", {"name": "Docs", "description": "d", "documents": '["policy text"]',
                                                "set_id": str(self.sset.id)})
        exported = self.client.get(f"/scenarios/{self.sset.id}/export/")
        self.assertEqual(exported.status_code, 200)
        target = ScenarioSet.objects.create(project=self.project, name="T")
        resp = self.client.post(f"/scenarios/{target.id}/import/", exported.content, content_type="application/json")
        self.assertEqual(resp.status_code, 302, resp.content)
        latest = target.versions.order_by("-version").first()
        self.assertEqual(latest.items.get().revision.documents, ["policy text"])

    def test_overlong_model_id_and_secret_ref_refused(self):
        from infra.tests.factories import ModelConnectionFactory
        from model_registry.models import RegisteredModel

        conn = ModelConnectionFactory(project=self.project)
        self.client.post("/connections/", {"action": "add_models", "conn_id": conn.id, "model_id": "m" * 300})
        self.assertFalse(RegisteredModel.objects.filter(connection=conn).exists())
        self.client.post("/connections/", {"action": "edit_connection", "conn_id": conn.id, "conn_name": conn.name,
                                           "conn_base_url": "https://x.example/v1", "conn_provider": "openai",
                                           "key_mode": "env", "conn_secret_ref": "E" * 300})
        conn.refresh_from_db()
        self.assertLessEqual(len(conn.secret_reference), 250)


class RemainingFormsBoundaryTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rf", password="pass12345")
        self.project = Project.objects.create(name="RF", slug="rf")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        self.client.raise_request_exception = False

    def test_profile_refuses_invalid_fields(self):
        for data in ({"username": "u" * 200}, {"username": "rf", "email": "not an email"},
                     {"username": "a b/c"}, {"username": "rf", "last_name": "x" * 300}):
            self.client.post("/profile/", data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "rf")
        self.assertEqual(self.user.email, "")
        self.assertLessEqual(len(self.user.last_name), 150)

    def test_connection_provider_capped(self):
        from model_registry.models import ModelConnection

        self.client.post("/connections/", {"action": "add_connection", "conn_name": "p",
                                           "conn_base_url": "https://x.example/v1", "conn_provider": "p" * 500})
        for c in ModelConnection.objects.filter(project=self.project):
            self.assertLessEqual(len(c.provider), 120)

    def test_cron_and_first_run_bounds(self):
        from audits.monitors import validate_cron

        with self.assertRaises(ValueError):
            validate_cron("0 0 * * " + ",".join(["1"] * 80))

    def test_design_form_oversized_ids_do_not_500(self):
        huge = "9" * 30
        for field in ("scenario_version", "scenario_set", "target_model", "judge"):
            resp = self.client.post("/experiments/new/", {field: huge, "action": "design"})
            self.assertLess(resp.status_code, 500, field)


class LegacyUsernameProfileTest(TestCase):
    def test_existing_username_with_space_can_still_save_profile(self):
        user = User.objects.create_user(username="Jane Doe", password="pass12345")
        self.client.force_login(user)
        self.client.post("/profile/", {"username": "Jane Doe", "first_name": "Jane"})
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Jane")

    def test_new_registration_rejects_invalid_username(self):
        self.client.post("/register/", {"username": "Bad Name", "password": "Str0ng-pass-123"})
        self.assertFalse(User.objects.filter(username="Bad Name").exists())
