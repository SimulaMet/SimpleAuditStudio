from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from accounts.models import Project, ProjectMembership

from config.settings import DEMO_MODE

User = get_user_model()


class AuthProjectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="strong-pass-123")
        self.other = User.objects.create_user(username="bob", password="strong-pass-456")
        self.project = Project.objects.create(name="Research", slug="research")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)

    def test_register_creates_user_and_sets_session(self):
        response = self.client.post(
            "/api/auth/register/",
            {"username": "carol", "email": "carol@example.com", "password": "another-strong-pass"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(username="carol").exists())

    def test_me_requires_authentication(self):
        response = self.client.get("/api/auth/me/")
        self.assertEqual(response.status_code, 403)

    def test_member_can_list_project(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/projects/")
        self.assertEqual(response.status_code, 200)
        slugs = [item["slug"] for item in response.json()]
        self.assertIn("research", slugs)

    def test_non_member_cannot_access_project_detail(self):
        self.client.force_login(self.other)
        response = self.client.get(f"/api/projects/{self.project.id}/")
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_create_project(self):
        admin = User.objects.create_superuser(username="root", email="root@example.com", password="admin-pass-123")
        self.client.force_login(admin)
        response = self.client.post("/api/projects/create/", {"name": "Ops", "slug": "ops"})
        self.assertEqual(response.status_code, 201)
        project = Project.objects.get(slug="ops")
        self.assertTrue(ProjectMembership.objects.filter(project=project, user=admin, role="admin").exists())

    def test_add_member_requires_superuser_or_admin(self):
        self.client.force_login(self.user)
        response = self.client.post(
            f"/api/projects/{self.project.id}/members/add/",
            {"username": "bob", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 403)


class LoginCSRFTests(TestCase):
    """Login must work when the app is embedded in an HF Space iframe.

    The Space page on huggingface.co frames the app served from *.hf.space.
    Form POSTs from inside that frame carry Origin: https://huggingface.co,
    which must be a trusted CSRF origin in demo mode or login fails with 403.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="demo", password="pass")

    @override_settings(DEMO_MODE=True)
    def test_login_from_hf_space_embed_origin(self):
        client = Client()
        # GET first so the CSRF cookie is issued for the app origin.
        client.get("/login/")
        response = client.post(
            "/login/",
            {"username": "demo", "password": "pass"},
            HTTP_ORIGIN="https://huggingface.co",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/dashboard/")

    def test_demo_mode_uses_cross_site_cookie_policy(self):
        # In demo mode (DEMO_MODE=true in env at settings load time) cookies
        # must be SameSite=None + Secure so they are sent on cross-site POSTs
        # from inside the huggingface.co iframe.
        if not DEMO_MODE:
            self.skipTest("requires DEMO_MODE=true environment")
        from django.conf import settings as dj_settings

        self.assertEqual(dj_settings.SESSION_COOKIE_SAMESITE, "None")
        self.assertEqual(dj_settings.CSRF_COOKIE_SAMESITE, "None")
        self.assertTrue(dj_settings.SESSION_COOKIE_SECURE)
        self.assertTrue(dj_settings.CSRF_COOKIE_SECURE)

    def test_normal_mode_keeps_strict_cookie_policy(self):
        # Outside demo mode the strict Lax default must remain in place.
        if DEMO_MODE:
            self.skipTest("requires DEMO_MODE=false environment")
        from django.conf import settings as dj_settings

        self.assertEqual(dj_settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(dj_settings.CSRF_COOKIE_SAMESITE, "Lax")
        self.assertFalse(dj_settings.SESSION_COOKIE_SECURE)
        self.assertFalse(dj_settings.CSRF_COOKIE_SECURE)
