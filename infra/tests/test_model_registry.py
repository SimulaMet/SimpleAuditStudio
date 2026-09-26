from django.test import TestCase


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
        except Exception as exc:  # noqa: BLE001 - test helper captures any validation error
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
