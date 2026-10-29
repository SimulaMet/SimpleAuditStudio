"""Regression tests for live demo wiring."""

from types import SimpleNamespace
from unittest.mock import patch


def test_demo_wiring_uses_registered_simula_model_id_for_agent_base():
    """The agent base must match the configured model, not a hard-coded default."""
    from infra.demo_audit import _ensure_simula_in_owui

    class API:
        def push_connections(self, payload):
            self.payload = payload

    api = API()
    connection = SimpleNamespace(id=2)

    with patch("chat.api.connection_payload", return_value={"id": 2}):
        assert _ensure_simula_in_owui(api, connection, "copilot-auto-efficiency") == (
            "2.copilot-auto-efficiency"
        )
