"""Worker client selection tests."""

from __future__ import annotations

from unittest.mock import patch


def test_placeholder_client_reconnects_to_persisted_embedded_handshake():
    """A separate command process must adopt the running embedded engine."""
    from infra import worker

    handshake = (
        '{"token":"embedded-token","tenant_id":"tenant",'
        '"grpc_address":"127.0.0.1:61234",'
        '"api_url":"http://127.0.0.1:61235"}'
    )
    placeholder = object()
    embedded = object()

    with (
        patch.object(worker, "_CLIENT", placeholder),
        patch.object(worker, "_CLIENT_IS_PLACEHOLDER", True),
        patch.object(worker, "_resolve_hatchet_token", return_value=None),
        patch(
            "infra.minimal_config.get_persisted_embedded_handshake",
            return_value=handshake,
        ),
        patch.object(worker, "ClientConfig"),
        patch.object(worker, "Hatchet", return_value=embedded) as hatchet,
    ):
        assert worker.get_client() is embedded

    hatchet.assert_called_once()
