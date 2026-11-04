"""Regression tests for authorization and secret-handling defects found in the system audit."""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from accounts.models import Project, ProjectMembership, User
from audits.models import Experiment
from model_registry.api_credential_services import upsert_credential, verify_bearer
from model_registry.services import (
    _same_origin,
    connection_api_key,
    is_platform_secret_name,
)


class ConnectionKeyTest(TestCase):
    def test_platform_secret_is_never_used_as_connection_key(self):
        conn = SimpleNamespace(api_key_direct="", secret_reference="DJANGO_SECRET_KEY", base_url="https://x.example/v1")
        with mock.patch.dict("os.environ", {"DJANGO_SECRET_KEY": "s3cret"}):
            self.assertEqual(connection_api_key(conn), "")

    def test_model_key_env_var_still_resolves(self):
        conn = SimpleNamespace(api_key_direct="", secret_reference="OPENAI_API_KEY", base_url="https://x.example/v1")
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-1"}):
            self.assertEqual(connection_api_key(conn), "sk-1")

    def test_platform_names(self):
        for name in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD", "HATCHET_CLIENT_TOKEN", "workos_api_key"):
            self.assertTrue(is_platform_secret_name(name), name)
        self.assertFalse(is_platform_secret_name("OPENROUTER_API_KEY"))

    def test_upstream_match_is_exact_origin(self):
        upstream = "http://open-webui:8080/chat"
        self.assertTrue(_same_origin("http://open-webui:8080/chat/api", upstream))
        self.assertFalse(_same_origin("http://open-webui:8080@attacker.example/v1", upstream))
        self.assertFalse(_same_origin("http://open-webui:8080.attacker.example/v1", upstream))


class LoginRedirectTest(TestCase):
    def test_backslash_next_is_not_followed(self):
        User.objects.create_user(username="bob", password="pass12345")
        resp = self.client.post("/login/?next=/%5Cevil.example", {"username": "bob", "password": "pass12345"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example", resp["Location"])

    def test_local_next_is_followed(self):
        User.objects.create_user(username="bob", password="pass12345")
        resp = self.client.post("/login/?next=/runs/", {"username": "bob", "password": "pass12345"})
        self.assertEqual(resp["Location"], "/runs/")


class ExperimentVisibilityTest(TestCase):
    def test_admin_of_other_workspace_cannot_publish(self):
        user = User.objects.create_user(username="eve", password="pass12345")
        shared = Project.objects.create(name="Shared", slug="shared")
        own = Project.objects.create(name="Own", slug="own")
        ProjectMembership.objects.create(project=shared, user=user, role=ProjectMembership.Role.VIEWER)
        ProjectMembership.objects.create(project=own, user=user, role=ProjectMembership.Role.ADMIN)
        exp = Experiment.objects.create(project=shared, name="E")
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = shared.id
        session.save()
        resp = self.client.post(f"/experiments/{exp.id}/set-visibility/", {"visibility": "public"})
        self.assertEqual(resp.status_code, 403)
        exp.refresh_from_db()
        self.assertEqual(exp.visibility, Experiment.Visibility.PRIVATE)


class BearerRevocationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="carol", password="pass12345")
        self.project = Project.objects.create(name="P", slug="p")
        self.membership = ProjectMembership.objects.create(
            project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR
        )
        self.token = upsert_credential(project=self.project, user=self.user).secret

    def test_active_member_key_works(self):
        self.assertIsNotNone(verify_bearer(self.token))

    def test_removed_member_key_stops_working(self):
        self.membership.delete()
        self.assertIsNone(verify_bearer(self.token))

    def test_deactivated_user_key_stops_working(self):
        self.user.is_active = False
        self.user.save()
        self.assertIsNone(verify_bearer(self.token))


class CsvSafeTest(TestCase):
    def test_formula_cells_are_neutralised(self):
        from infra.export_utils import csv_safe

        self.assertEqual(csv_safe("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(csv_safe("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(csv_safe("plain"), "plain")
        self.assertEqual(csv_safe(-3), -3)


class ScenarioImportViewTest(TestCase):
    def setUp(self):
        from scenarios.models import ScenarioSet

        self.user = User.objects.create_user(username="imp", password="pass12345")
        self.project = Project.objects.create(name="I", slug="i")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sset = ScenarioSet.objects.create(project=self.project, name="S")
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def _post(self, body):
        import json

        return self.client.post(f"/scenarios/{self.sset.id}/import/", json.dumps(body), content_type="application/json")

    def test_non_list_scenarios_rejected(self):
        self.assertEqual(self._post({"scenarios": "abc"}).status_code, 400)

    def test_keyless_items_get_distinct_scenarios(self):
        from scenarios.models import Scenario

        items = [{"title": f"T{i}", "description": "d", "expected_behavior": ["x"]} for i in range(3)]
        self.assertEqual(self._post({"scenarios": items}).status_code, 302)
        self.assertEqual(Scenario.objects.filter(project=self.project).count(), 3)

    def test_failure_rolls_back_whole_import(self):
        from scenarios.models import Scenario

        items = [{"key": "ok", "title": "OK", "description": "d", "expected_behavior": ["x"]}, {"key": "bad"}]
        with mock.patch("infra.ui._publish_new_version", side_effect=RuntimeError("boom")):
            self.assertEqual(self._post({"scenarios": items}).status_code, 400)
        self.assertFalse(Scenario.objects.filter(project=self.project).exists())


class AgentWriteGuardTest(TestCase):
    def setUp(self):
        from infra.tests.factories import AgentFactory

        self.viewer = User.objects.create_user(username="view", password="pass12345")
        self.project = Project.objects.create(name="A", slug="a")
        ProjectMembership.objects.create(project=self.project, user=self.viewer, role=ProjectMembership.Role.VIEWER)
        self.agent = AgentFactory(project=self.project)
        self.client.force_login(self.viewer)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_viewer_cannot_delete_agent(self):
        from model_registry.models import Agent

        self.client.post(f"/agents/{self.agent.id}/delete/")
        self.assertTrue(Agent.objects.filter(pk=self.agent.pk).exists())

    def test_viewer_cannot_create_agent(self):
        from model_registry.models import Agent

        before = Agent.objects.count()
        self.client.post("/agents/new/", {"name": "X", "base_model": self.agent.base_model_id})
        self.assertEqual(Agent.objects.count(), before)

    def test_viewer_cannot_sync(self):
        self.assertEqual(self.client.post("/agents/sync/").status_code, 403)


class RegistrationValidationTest(TestCase):
    def test_duplicate_email_rejected(self):
        User.objects.create_user(username="victim", password="pass12345", email="v@example.com")
        self.client.post("/register/", {"username": "att", "password": "Str0ng-pass-123", "email": "V@example.com"})
        self.assertFalse(User.objects.filter(username="att").exists())

    def test_weak_password_rejected(self):
        self.client.post("/register/", {"username": "newbie", "password": "1"})
        self.assertFalse(User.objects.filter(username="newbie").exists())

    def test_case_variant_username_rejected(self):
        User.objects.create_user(username="alice", password="pass12345")
        self.client.post("/register/", {"username": "Alice", "password": "Str0ng-pass-123"})
        self.assertEqual(User.objects.filter(username__iexact="alice").count(), 1)

    def test_valid_registration_succeeds(self):
        resp = self.client.post("/register/", {"username": "fresh", "password": "Str0ng-pass-123", "email": "f@example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(User.objects.filter(username="fresh").exists())


class ConnectionsPageQueryCountTest(TestCase):
    """The connections page must not run queries per connection (N+1)."""

    def _count(self, n):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from infra.tests.factories import ModelConnectionFactory, RegisteredModelFactory

        for _ in range(n):
            conn = ModelConnectionFactory(project=self.project)
            RegisteredModelFactory(project=self.project, connection=conn)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get("/connections/").status_code, 200)
        return len(ctx)

    def setUp(self):
        self.user = User.objects.create_user(username="cq", password="pass12345")
        self.project = Project.objects.create(name="Q", slug="q")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_query_count_flat_in_connection_count(self):
        small = self._count(2)
        large = self._count(8)
        self.assertEqual(small, large)


class EmbeddingSearchLimitTest(TestCase):
    def test_bad_limit_does_not_500(self):
        admin = User.objects.create_superuser(username="su", password="pass12345")
        self.client.force_login(admin)
        fake = mock.Mock(status_code=200, json=list)
        fake.raise_for_status = lambda: None
        with mock.patch("requests.get", return_value=fake) as get:
            resp = self.client.get("/api/admin/embedding-models/search/?limit=abc")
        self.assertNotEqual(resp.status_code, 500)
        self.assertEqual(get.call_args.kwargs["params"]["limit"], "15")


class OtlpCredentialApiTest(TestCase):
    def setUp(self):
        from infra.tests.factories import ModelConnectionFactory

        self.user = User.objects.create_user(username="ot", password="pass12345")
        self.project = Project.objects.create(name="O", slug="o")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.conn = ModelConnectionFactory(project=self.project)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_revoke_returns_ok_and_disables(self):
        from model_registry import otlp_services as otlp

        cred = otlp.create_credential(project=self.project, connection=self.conn, auth_mode="basic", user=self.user).credential
        resp = self.client.post(f"/api/otlp/credentials/{cred.id}/revoke/")
        self.assertEqual(resp.status_code, 200)
        cred.refresh_from_db()
        self.assertFalse(cred.enabled)

    def test_create_with_bad_auth_mode_is_400(self):
        for mode in ("digest", 1):
            resp = self.client.post(
                "/api/otlp/credentials/create/",
                {"connection_id": self.conn.id, "auth_mode": mode},
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 400, mode)


class OpenWebUIEnvTest(TestCase):
    def test_studio_secrets_not_inherited(self):
        from chat.proxy import _owui_inherited_env

        env = {"DJANGO_SECRET_KEY": "s", "POSTGRES_PASSWORD": "p", "DATABASE_URL": "d", "WORKOS_API_KEY": "w",
               "BOOTSTRAP_PASSWORD": "b", "HATCHET_CLIENT_TOKEN": "h", "PATH": "/bin", "ENABLE_RAG_HYBRID_SEARCH": "1"}
        with mock.patch.dict("os.environ", env, clear=True):
            out = _owui_inherited_env()
        self.assertEqual(out, {"PATH": "/bin", "ENABLE_RAG_HYBRID_SEARCH": "1"})


class RemovedMemberSessionTest(TestCase):
    def test_removed_member_loses_active_workspace(self):
        from audits.models import AuditRun  # noqa: F401  (ensures app loaded)

        user = User.objects.create_user(username="bob", password="pass12345")
        team = Project.objects.create(name="Team", slug="team")
        membership = ProjectMembership.objects.create(project=team, user=user, role=ProjectMembership.Role.AUDITOR)
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = team.id
        session.save()
        self.assertEqual(self.client.get("/runs/").wsgi_request.project, team)
        membership.delete()
        resp = self.client.get("/runs/")
        self.assertNotEqual(resp.wsgi_request.project, team)
        self.assertNotEqual(self.client.session.get("active_project_id"), team.id)


class MockEndpointBackupTest(TestCase):
    def setUp(self):
        from infra.tests.factories import ModelConnectionFactory

        self.project = Project.objects.create(name="M", slug="m")
        self.ollama = ModelConnectionFactory(project=self.project, base_url="http://localhost:11434/v1", api_key_direct="")
        self.anthropic = ModelConnectionFactory(
            project=self.project, base_url="https://api.anthropic.com/v1", api_key_direct="sk-ant"
        )

    def test_mock_run_then_normal_start_restores_everything(self):
        from simpleaudit_studio.cli import (
            _restore_real_model_endpoints,
            _update_model_endpoints,
        )

        _update_model_endpoints("http://127.0.0.1:50001/v1")
        _update_model_endpoints("http://127.0.0.1:50002/v1")  # a second mock run keeps the first backup
        self.anthropic.refresh_from_db()
        self.assertEqual(self.anthropic.api_key_direct, "sk-ant")
        _restore_real_model_endpoints()
        self.ollama.refresh_from_db()
        self.anthropic.refresh_from_db()
        self.assertEqual(self.ollama.base_url, "http://localhost:11434/v1")
        self.assertEqual(self.anthropic.base_url, "https://api.anthropic.com/v1")
        self.assertEqual(self.anthropic.api_key_direct, "sk-ant")

    def test_url_edited_during_mock_run_is_kept(self):
        from simpleaudit_studio.cli import (
            _restore_real_model_endpoints,
            _update_model_endpoints,
        )

        _update_model_endpoints("http://127.0.0.1:50001/v1")
        type(self.ollama).objects.filter(pk=self.ollama.pk).update(base_url="http://localhost:11500/v1")
        _restore_real_model_endpoints()
        self.ollama.refresh_from_db()
        self.anthropic.refresh_from_db()
        self.assertEqual(self.ollama.base_url, "http://localhost:11500/v1")
        self.assertEqual(self.anthropic.base_url, "https://api.anthropic.com/v1")

    def test_edit_between_two_mock_runs_is_kept(self):
        from simpleaudit_studio.cli import (
            _restore_real_model_endpoints,
            _update_model_endpoints,
        )

        _update_model_endpoints("http://127.0.0.1:50001/v1")
        type(self.ollama).objects.filter(pk=self.ollama.pk).update(base_url="http://localhost:11500/v1")
        _update_model_endpoints("http://127.0.0.1:50002/v1")
        _restore_real_model_endpoints()
        self.ollama.refresh_from_db()
        self.anthropic.refresh_from_db()
        self.assertEqual(self.ollama.base_url, "http://localhost:11500/v1")
        self.assertEqual(self.anthropic.base_url, "https://api.anthropic.com/v1")

    def test_normal_start_leaves_local_endpoints_alone(self):
        from simpleaudit_studio.cli import _restore_real_model_endpoints

        _restore_real_model_endpoints()
        self.ollama.refresh_from_db()
        self.assertEqual(self.ollama.base_url, "http://localhost:11434/v1")


class AgentApiWriteGuardTest(TestCase):
    """The DRF agents/knowledge-bases/tools API must refuse viewer and archived-workspace writes."""

    def _login(self, role, archived=False):
        from infra.tests.factories import AgentFactory

        user = User.objects.create_user(username=f"u-{role}-{archived}", password="pass12345")
        project = Project.objects.create(name=f"P{role}{archived}", slug=f"p-{role}-{archived}", archived=archived)
        ProjectMembership.objects.create(project=project, user=user, role=role)
        self.agent = AgentFactory(project=project)
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = project.id
        session.save()

    def _assert_no_writes(self):
        from model_registry.models import Agent

        before = Agent.objects.count()
        self.assertEqual(self.client.post("/api/agents/", {"name": "x"}, content_type="application/json").status_code, 403)
        self.assertEqual(self.client.put(
            f"/api/agents/{self.agent.id}/", {"name": "y"}, content_type="application/json").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/agents/{self.agent.id}/").status_code, 403)
        self.assertEqual(self.client.post("/api/knowledge-bases/", {"name": "k"}, content_type="application/json").status_code, 403)
        self.assertEqual(self.client.post("/api/tools/", {"name": "t"}, content_type="application/json").status_code, 403)
        self.assertEqual(Agent.objects.count(), before)
        self.agent.refresh_from_db()
        self.assertNotEqual(self.agent.name, "y")

    def test_viewer_cannot_write(self):
        self._login(ProjectMembership.Role.VIEWER)
        self._assert_no_writes()

    def test_archived_workspace_admin_cannot_write(self):
        self._login(ProjectMembership.Role.ADMIN, archived=True)
        self._assert_no_writes()

    def test_viewer_can_still_read(self):
        self._login(ProjectMembership.Role.VIEWER)
        self.assertEqual(self.client.get("/api/agents/").status_code, 200)


class ConnectionShareScopeTest(TestCase):
    def test_cannot_share_into_workspace_not_administered(self):
        from infra.tests.factories import ModelConnectionFactory

        user = User.objects.create_user(username="sh", password="pass12345")
        own = Project.objects.create(name="Own", slug="own-s")
        mine2 = Project.objects.create(name="Mine2", slug="mine2")
        victim = Project.objects.create(name="Victim", slug="victim")
        ProjectMembership.objects.create(project=own, user=user, role=ProjectMembership.Role.ADMIN)
        ProjectMembership.objects.create(project=mine2, user=user, role=ProjectMembership.Role.ADMIN)
        conn = ModelConnectionFactory(project=own, base_url="https://x.example/v1")
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = own.id
        session.save()
        self.client.post("/connections/", {
            "action": "edit_connection", "conn_id": conn.id, "conn_name": conn.name,
            "conn_base_url": "https://x.example/v1", "conn_provider": "openai", "key_mode": "none",
            "conn_visibility": "admins", "conn_shared_with": [str(victim.id), str(mine2.id)],
        })
        shared = set(conn.shared_with.values_list("pk", flat=True))
        self.assertIn(mine2.id, shared)   # the edit went through
        self.assertNotIn(victim.id, shared)


class ArchivedWorkspaceWritesTest(TestCase):
    def setUp(self):
        from infra.tests.factories import ModelConnectionFactory

        self.user = User.objects.create_user(username="arc", password="pass12345")
        self.project = Project.objects.create(name="Arc", slug="arc", archived=True)
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.conn = ModelConnectionFactory(project=self.project)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_otlp_credential_create_refused(self):
        from model_registry.models import OTLPCredential

        resp = self.client.post("/api/otlp/credentials/create/", {"connection_id": self.conn.id},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(OTLPCredential.objects.exists())

    def test_archived_workspace_delete_refused(self):
        from accounts.services import delete_workspace
        from infra.exceptions import StableAPIError

        with self.assertRaises(StableAPIError):
            delete_workspace(user=self.user, project=self.project)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())


class RemoteDeleteActorTest(TestCase):
    def test_remote_delete_runs_as_creator_or_requester_not_superuser(self):
        from model_registry.services import _remote_actor

        User.objects.create_superuser(username="root", password="pass12345")
        requester = User.objects.create_user(username="req", password="pass12345")
        creator = User.objects.create_user(username="cre", password="pass12345")
        from types import SimpleNamespace

        self.assertEqual(_remote_actor(SimpleNamespace(created_by=creator), requester), creator)
        self.assertEqual(_remote_actor(SimpleNamespace(created_by=None), requester), requester)


class WorkOSSignInTest(TestCase):
    def _wo(self, uid, email):
        from types import SimpleNamespace

        client = mock.Mock()
        client.user_management.authenticate_with_magic_auth.return_value = SimpleNamespace(
            user=SimpleNamespace(id=uid, email=email, first_name="", last_name="")
        )
        return mock.patch("accounts.workos_auth._client", return_value=client)

    def test_new_signup_refused_when_registration_disabled(self):
        from accounts.workos_auth import authenticate_magic_auth

        with self._wo("wo_1", "new@example.com"), self.assertRaises(ValueError):
            authenticate_magic_auth("c", "new@example.com", allow_create=False)
        self.assertFalse(User.objects.filter(workos_user_id="wo_1").exists())

    def test_existing_workos_user_can_sign_in_when_registration_disabled(self):
        from accounts.workos_auth import authenticate_magic_auth

        User.objects.create_user(username="w", email="w@example.com", workos_user_id="wo_2")
        with self._wo("wo_2", "w@example.com"):
            _user, created = authenticate_magic_auth("c", "w@example.com", allow_create=False)
        self.assertFalse(created)

    def test_email_collision_refused(self):
        from accounts.workos_auth import authenticate_magic_auth

        User.objects.create_user(username="squatter", password="pass12345", email="Victim@example.com")
        with self._wo("wo_3", "victim@example.com"), self.assertRaises(ValueError):
            authenticate_magic_auth("c", "victim@example.com")


class AddMemberRoleBypassTest(TestCase):
    def test_add_member_cannot_change_existing_role(self):
        admin = User.objects.create_user(username="ad", password="pass12345")
        other = User.objects.create_user(username="ot2", password="pass12345")
        project = Project.objects.create(name="M2", slug="m2")
        ProjectMembership.objects.create(project=project, user=admin, role=ProjectMembership.Role.ADMIN)
        ProjectMembership.objects.create(project=project, user=other, role=ProjectMembership.Role.VIEWER)
        self.client.force_login(admin)
        resp = self.client.post(f"/api/projects/{project.id}/members/add/", {"username": "ad", "role": "viewer"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(ProjectMembership.objects.get(project=project, user=admin).role, ProjectMembership.Role.ADMIN)


class SnapshotKeyOriginTest(TestCase):
    """A run sends its key only to the host the key was resolved for."""

    def test_repointed_connection_yields_no_key(self):
        from infra.engine import snapshot_api_key
        from infra.tests.factories import ModelConnectionFactory

        project = Project.objects.create(name="SK", slug="sk")
        conn = ModelConnectionFactory(project=project, base_url="http://127.0.0.1:8080/chat/api/v1", api_key_direct="sk-x")
        frozen = {"connection_id": conn.id, "base_url": "https://attacker.example/v1"}
        self.assertIsNone(snapshot_api_key(frozen))

    def test_same_origin_still_resolves(self):
        from infra.engine import snapshot_api_key
        from infra.tests.factories import ModelConnectionFactory

        project = Project.objects.create(name="SK2", slug="sk2")
        conn = ModelConnectionFactory(project=project, base_url="https://api.example.com/v1", api_key_direct="sk-y")
        self.assertEqual(snapshot_api_key({"connection_id": conn.id, "base_url": "https://api.example.com/v1/"}), "sk-y")


class AgentBaseKeyOriginTest(TestCase):
    def test_repointed_agent_base_connection_gets_no_key(self):
        from infra.engine import auditor_kwargs, augment_agent_generation
        from infra.tests.factories import ModelConnectionFactory

        project = Project.objects.create(name="AB", slug="ab")
        conn = ModelConnectionFactory(project=project, base_url="http://127.0.0.1:8080/chat/api/v1", api_key_direct="sk-live")
        agent_snapshot = {"base_model": {"connection_id": conn.id, "model_id": "m", "provider": "openai",
                                         "base_url": "https://attacker.example/v1", "default_parameters": {}}}
        gen = augment_agent_generation({}, agent_snapshot)
        snap = {"model_id": "studio.agent-1", "provider": "openai", "base_url": "http://w.local/v1"}
        kwargs, _ = auditor_kwargs(target=snap, auditor=snap, judge=snap, generation=gen)
        self.assertEqual(kwargs["base_url"], "https://attacker.example/v1")
        self.assertNotEqual(kwargs["api_key"], "sk-live")


class ScenarioKeyCollisionTest(TestCase):
    def setUp(self):
        from scenarios.models import ScenarioSet

        self.user = User.objects.create_user(username="sk1", password="pass12345")
        self.project = Project.objects.create(name="SC", slug="sc")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sset = ScenarioSet.objects.create(project=self.project, name="S")
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()
        self.client.raise_request_exception = False

    def test_same_title_twice_does_not_500(self):
        from scenarios.models import Scenario

        for _ in range(2):
            resp = self.client.post("/scenarios/create/", {"name": "dup", "set_id": str(self.sset.id), "description": "d"})
            self.assertLess(resp.status_code, 500)
        self.assertEqual(Scenario.objects.filter(project=self.project, title="dup").count(), 2)

    def test_reimport_of_archived_key_restores_it(self):
        import json

        from django.utils import timezone

        from scenarios.models import Scenario

        Scenario.objects.create(project=self.project, key="K", title="old", archived_at=timezone.now())
        body = {"scenarios": [{"key": "K", "title": "Reimport", "description": "d", "expected_behavior": ["x"]}]}
        self.client.post(f"/scenarios/{self.sset.id}/import/", json.dumps(body), content_type="application/json")
        scenario = Scenario.objects.get(project=self.project, key="K")
        self.assertIsNone(scenario.archived_at)
        latest = self.sset.versions.order_by("-version").first()
        self.assertTrue(latest.items.filter(scenario=scenario).exists())


class ScenarioRevertTest(TestCase):
    def setUp(self):
        from scenarios.models import ScenarioSet
        from scenarios.services import create_scenario

        self.user = User.objects.create_user(username="rv", password="pass12345")
        self.project = Project.objects.create(name="RV", slug="rv")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.sset = ScenarioSet.objects.create(project=self.project, name="S")
        self.a = create_scenario(project=self.project, user=self.user, key="a", title="A", description="old",
                                 expected_behavior=["x"])
        self.c = create_scenario(project=self.project, user=self.user, key="c", title="C", description="c",
                                 expected_behavior=["x"])
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.project.id
        session.save()

    def test_revert_survives_an_edit_of_another_scenario(self):
        from scenarios.services import (
            publish_scenario_set_version,
            update_scenario_content,
        )

        publish_scenario_set_version(scenario_set=self.sset, user=self.user, scenario_ids=[self.a.id, self.c.id])
        update_scenario_content(scenario=self.a, user=self.user, description="BAD", expected_behavior=["x"])
        publish_scenario_set_version(scenario_set=self.sset, user=self.user, scenario_ids=[self.a.id, self.c.id])
        self.client.post(f"/scenarios/revert/{self.sset.id}/", {"target_version": "1"})
        # An ordinary edit of C publishes the next version: A must stay reverted.
        self.client.post(f"/scenarios/edit/{self.c.id}/", {
            "title": "C", "description": "c2", "expected_behavior": "x", "set_id": str(self.sset.id),
        })
        latest = self.sset.versions.order_by("-version").first()
        items = {i.scenario_id: i.revision.description for i in latest.items.select_related("revision")}
        self.assertEqual(items[self.a.id], "old")
        self.assertEqual(items[self.c.id], "c2")

    def test_revert_restores_old_text_and_deleted_scenarios(self):
        from django.utils import timezone

        from scenarios.services import (
            publish_scenario_set_version,
            update_scenario_content,
        )

        publish_scenario_set_version(scenario_set=self.sset, user=self.user, scenario_ids=[self.a.id, self.c.id])
        update_scenario_content(scenario=self.a, user=self.user, description="NEW", expected_behavior=["x"])
        type(self.c).objects.filter(pk=self.c.pk).update(archived_at=timezone.now())
        publish_scenario_set_version(scenario_set=self.sset, user=self.user, scenario_ids=[self.a.id])

        self.client.post(f"/scenarios/revert/{self.sset.id}/", {"target_version": "1"})
        latest = self.sset.versions.order_by("-version").first()
        self.assertEqual(latest.version, 3)
        items = {i.scenario_id: i.revision for i in latest.items.select_related("revision")}
        self.assertEqual(items[self.a.id].description, "old")
        self.assertIn(self.c.id, items)
        self.c.refresh_from_db()
        self.assertIsNone(self.c.archived_at)


class JudgeGuidanceAndPromptPinTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="jg", password="pass12345")
        self.project = Project.objects.create(name="JG", slug="jg")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.ADMIN)

    def test_agentic_guidance_survives_create_and_spec(self):
        from judges.services import create_judge, make_spec

        judge = create_judge(project=self.project, name="J", user=self.user, output="severity", criteria="C",
                             probe_prompt="", options={"agentic_guidance": "Keep me"})
        self.assertEqual(judge.latest.options.get("agentic_guidance"), "Keep me")
        spec = make_spec(base="safety", options={"agentic_guidance": "G"})
        self.assertEqual(spec["options"]["agentic_guidance"], "G")

    def test_prompt_version_pinned_by_run_cannot_be_deleted(self):
        from audits.models import AuditRun
        from infra.tests.factories import AuditRunFactory
        from prompts.models import SystemPrompt
        from prompts.services import delete_version

        prompt = SystemPrompt.objects.create(project=self.project, name="P")
        v1 = prompt.versions.create(version=1, content="A ")
        prompt.versions.create(version=2, content="B")
        run = AuditRunFactory(project=self.project)
        AuditRun.objects.filter(pk=run.pk).update(system_prompt_version=v1)
        with self.assertRaises(ValueError):
            delete_version(v1)


class SharedScenarioDeleteTest(TestCase):
    def test_delete_in_one_set_keeps_it_in_the_other(self):
        from scenarios.models import ScenarioSet
        from scenarios.services import create_scenario, publish_scenario_set_version

        user = User.objects.create_user(username="sd", password="pass12345")
        project = Project.objects.create(name="SD", slug="sd")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectMembership.Role.ADMIN)
        src = ScenarioSet.objects.create(project=project, name="Src")
        dst = ScenarioSet.objects.create(project=project, name="Dst")
        shared = create_scenario(project=project, user=user, key="s", title="S", description="d", expected_behavior=["x"])
        other = create_scenario(project=project, user=user, key="o", title="O", description="d", expected_behavior=["x"])
        publish_scenario_set_version(scenario_set=src, user=user, scenario_ids=[shared.id, other.id])
        publish_scenario_set_version(scenario_set=dst, user=user, scenario_ids=[shared.id, other.id])
        self.client.force_login(user)
        session = self.client.session
        session["active_project_id"] = project.id
        session.save()
        self.client.post(f"/scenarios/delete/{shared.id}/", {"set_id": str(dst.id)})
        dst_latest = dst.versions.order_by("-version").first()
        self.assertFalse(dst_latest.items.filter(scenario=shared).exists())
        shared.refresh_from_db()
        self.assertIsNone(shared.archived_at)   # still used by Src
        self.client.post(f"/scenarios/edit/{other.id}/", {"title": "O", "description": "d2", "expected_behavior": "x",
                                                         "set_id": str(src.id)})
        src_latest = src.versions.order_by("-version").first()
        self.assertTrue(src_latest.items.filter(scenario=shared).exists())


class ConnectionShareLevelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="sl", password="pass12345")
        self.own = Project.objects.create(name="SL", slug="sl")
        self.other = Project.objects.create(name="SL2", slug="sl2")
        for p in (self.own, self.other):
            ProjectMembership.objects.create(project=p, user=self.user, role=ProjectMembership.Role.ADMIN)
        self.client.force_login(self.user)
        session = self.client.session
        session["active_project_id"] = self.own.id
        session.save()

    def _post(self, **extra):
        data = {"conn_name": "C", "conn_base_url": "https://x.example/v1", "conn_provider": "openai",
                "key_mode": "none", "conn_shared_with": [str(self.other.id)]}
        data.update(extra)
        return self.client.post("/connections/", data)

    def test_add_honours_shares_and_workspace_only_clears_them(self):
        from model_registry.models import ModelConnection

        self._post(action="add_connection", conn_visibility="admins")
        conn = ModelConnection.objects.get(project=self.own, name="C")
        self.assertEqual(list(conn.shared_with.values_list("pk", flat=True)), [self.other.id])
        self._post(action="edit_connection", conn_id=conn.id, conn_visibility="workspace")
        self.assertFalse(conn.shared_with.exists())


class PasswordChangeRevokesTokensTest(TestCase):
    def test_profile_and_admin_password_change_drop_auth_tokens(self):
        from rest_framework.authtoken.models import Token

        from accounts.services import admin_update_user

        user = User.objects.create_user(username="pw", password="Old-pass-123x")
        Token.objects.create(user=user)
        self.client.force_login(user)
        self.client.post("/profile/", {"form": "password", "current_password": "Old-pass-123x",
                                       "new_password": "New-pass-456y", "confirm_password": "New-pass-456y"})
        user.refresh_from_db()
        self.assertTrue(user.check_password("New-pass-456y"))
        self.assertFalse(Token.objects.filter(user=user).exists())

        Token.objects.create(user=user)
        admin = User.objects.create_superuser(username="root2", password="pass12345")
        admin_update_user(admin_user=admin, target=user, password="Reset-pass-789z")
        self.assertFalse(Token.objects.filter(user=user).exists())


class RunNowOwnerCheckTest(TestCase):
    def test_run_now_refused_when_creator_lost_role(self):
        from unittest import mock

        from audits.models import AuditRun, Monitor
        from infra.tests.factories import AuditRunFactory

        admin = User.objects.create_user(username="adm", password="pass12345")
        creator = User.objects.create_user(username="cr", password="pass12345")
        project = Project.objects.create(name="RN", slug="rn")
        ProjectMembership.objects.create(project=project, user=admin, role=ProjectMembership.Role.ADMIN)
        template = AuditRunFactory(project=project)
        monitor = Monitor.objects.create(
            project=project, name="M", scenario_set=template.scenario_set_version.scenario_set,
            scenario_set_version=template.scenario_set_version, target_model=template.target_model,
            auditor_model=template.auditor_model, judge_model=template.judge_model,
            judge=template.judge_version.judge, judge_version=template.judge_version,
            generation_parameters={}, interval_hours=24, created_by=creator,
        )
        self.client.force_login(admin)
        session = self.client.session
        session["active_project_id"] = project.id
        session.save()
        before = AuditRun.objects.count()
        with mock.patch("infra.ui.submit_audit_run"):
            self.client.post(f"/monitors/{monitor.id}/run-now/")
        self.assertEqual(AuditRun.objects.count(), before)


class SecondAdminWorkspaceOnlyTest(TestCase):
    def test_workspace_only_clears_shares_made_by_another_admin(self):
        from infra.tests.factories import ModelConnectionFactory

        a = User.objects.create_user(username="aa", password="pass12345")
        b = User.objects.create_user(username="bb", password="pass12345")
        w = Project.objects.create(name="W", slug="w-ws")
        x = Project.objects.create(name="X", slug="x-ws")
        ProjectMembership.objects.create(project=w, user=a, role=ProjectMembership.Role.ADMIN)
        ProjectMembership.objects.create(project=x, user=a, role=ProjectMembership.Role.ADMIN)
        ProjectMembership.objects.create(project=w, user=b, role=ProjectMembership.Role.ADMIN)
        conn = ModelConnectionFactory(project=w, name="C", base_url="https://x.example/v1", visibility="admins")
        conn.shared_with.set([x])
        self.client.force_login(b)
        session = self.client.session
        session["active_project_id"] = w.id
        session.save()
        self.client.post("/connections/", {"action": "edit_connection", "conn_id": conn.id, "conn_name": "C",
                                           "conn_base_url": "https://x.example/v1", "conn_provider": "openai",
                                           "key_mode": "none", "conn_visibility": "workspace"})
        conn.refresh_from_db()
        self.assertEqual(conn.visibility, "workspace")
        self.assertFalse(conn.shared_with.exists())


class SharedConnectionListedOnceTest(TestCase):
    def test_connection_shared_with_two_workspaces_appears_once(self):
        from infra.tests.factories import ModelConnectionFactory
        from model_registry.services import visible_connections_for

        admin = User.objects.create_user(username="dup", password="pass12345")
        owner, a, b = (Project.objects.create(name=n, slug=n) for n in ("own-d", "a-d", "b-d"))
        for p in (owner, a, b):
            ProjectMembership.objects.create(project=p, user=admin, role=ProjectMembership.Role.ADMIN)
        conn = ModelConnectionFactory(project=owner, visibility="admins")
        conn.shared_with.set([a, b])
        self.assertEqual([c.pk for c in visible_connections_for(admin, a)].count(conn.pk), 1)
