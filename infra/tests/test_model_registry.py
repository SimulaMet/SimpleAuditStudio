from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Project, ProjectMembership, User


class ModelRegistryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pass12345")
        self.project = Project.objects.create(name="Research", slug="research")
        ProjectMembership.objects.create(project=self.project, user=self.user, role=ProjectMembership.Role.AUDITOR)
        self.client.force_authenticate(user=self.user)

    def test_create_model_endpoint_stores_secret_reference_not_secret_value(self):
        response = self.client.post(
            f"/api/projects/{self.project.id}/model-endpoints/create/",
            {
                "display_name": "Qwen 3.8 27B",
                "provider": "simulachat",
                "base_url": "https://example.invalid/v1",
                "model_id": "qwen-3.8-27b",
                "secret_reference": "SIMULACHAT_API_KEY",
                "default_parameters": {"temperature": 0.7},
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        payload = response.json()
        assert payload["secret_reference"] == "SIMULACHAT_API_KEY"
        assert "api_key" not in payload

    def test_non_member_cannot_create_model_endpoint(self):
        outsider = User.objects.create_user(username="bob", password="pass12345")
        client = APIClient()
        client.force_authenticate(user=outsider)
        response = client.post(
            f"/api/projects/{self.project.id}/model-endpoints/create/",
            {"display_name": "X", "provider": "p", "base_url": "https://example.invalid/v1", "model_id": "m"},
            format="json",
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "project_access_denied"


class EndpointURLFieldTests(TestCase):
    """The base_url validator must accept internal container service names.

    A self-hosted deployment reaches model endpoints via single-label Docker
    Compose / k8s hostnames (e.g. ``mock-model``, ``postgres``). DRF's stock
    URLField rejects these, which would make every internal endpoint
    uncreatable. These tests pin that behaviour.
    """

    def _validate(self, url):
        from model_registry.serializers import EndpointURLField

        field = EndpointURLField()
        try:
            return field.run_validation(url), None
        except Exception as exc:
            return None, exc

    def test_accepts_single_label_service_name(self):
        value, err = self._validate("http://mock-model:8901/v1")
        assert err is None, err
        assert value == "http://mock-model:8901/v1"

    def test_accepts_dotted_domain_and_ip(self):
        for url in ("https://api.example.com/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/"):
            value, err = self._validate(url)
            assert err is None, (url, err)
            assert value == url

    def test_rejects_missing_scheme(self):
        _, err = self._validate("mock-model:8901/v1")
        assert err is not None

    def test_rejects_bad_port(self):
        _, err = self._validate("http://mock-model:notaport/v1")
        assert err is not None

    def test_rejects_empty(self):
        _, err = self._validate("")
        assert err is not None

    def test_rejects_malformed_host_leading_hyphen(self):
        _, err = self._validate("http://-bad:8000/v1")
        assert err is not None
